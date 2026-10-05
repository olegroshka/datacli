"""EVAL-009 (a): the hedge-fund crossings on the AFM long register as events (DD-006 C4).

The design is fixed in EVAL-009 before any return was computed. An event is
one holder's notification on one issuer; its direction is read against the
same holder's previous notification on that issuer (``up``: the total
capital interest rose, or a first notification at or above the 3 percent
threshold; ``down``: it fell, or a first notification below the threshold,
an exit; unchanged: no event). The entry is the first close at or after
``published_from``; the event return over ``h`` trading days is the
compounded clipped daily return of the issuer's series minus the market's
over the same days (the cumulative abnormal return, CAR). The two tests
read the mean CAR of the ``up`` and of the ``down`` events at 21 days with
a standard error clustered by the ISO week of ``published_from``,
Bonferroni over the two (p < 0.025).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev

THRESHOLD_PCT = 3.0
SAMPLE_START = date(2013, 1, 1)
LEDGER_START = date(2026, 10, 6)
HORIZON = 21
HORIZONS: tuple[int, ...] = (5, 21, 63)
PRE_WINDOW = 21
RETURN_CLIP = 0.5
FAMILY_SIZE = 2
MIN_EVENTS = 30
EVENT_KINDS: tuple[str, ...] = ("hedge_fund",)
NON_BANK_KINDS: tuple[str, ...] = ("other", "person")
ORIENTATION = {"up": 1.0, "down": -1.0}

EVENT_COLUMNS: tuple[str, ...] = (
    "holder",
    "issuer",
    "obligation_date",
    "published_from",
    "pct_capital",
    "prev_pct",
    "direction",
    "has_potential",
    "kind",
)


def crossings(
    notifications: pd.DataFrame,
    holders: pd.DataFrame,
    *,
    kinds: Sequence[str] = EVENT_KINDS,
    start: date = SAMPLE_START,
) -> pd.DataFrame:
    """The events of the holders of ``kinds`` (:data:`EVENT_COLUMNS`), directions read on the whole history, dated from ``start``.

    ``notifications`` carries ``published_from`` (DD-006 C3) besides the
    canonical columns; several notifications of one pair on one day keep the
    last.
    """
    kind_of = holders.set_index("holder")["kind"]
    frame = notifications[notifications["holder"].map(kind_of).isin(list(kinds))].copy()
    if frame.empty:
        return pd.DataFrame(columns=list(EVENT_COLUMNS))
    frame["kind"] = frame["holder"].map(kind_of)
    frame = frame.sort_values(["holder", "issuer", "obligation_date"], kind="stable").drop_duplicates(["holder", "issuer", "obligation_date"], keep="last")
    frame["prev_pct"] = frame.groupby(["holder", "issuer"], sort=False)["pct_capital"].shift(1)
    first = frame["prev_pct"].isna()
    pct = frame["pct_capital"]
    direction = np.where(
        first,
        np.where(pct >= THRESHOLD_PCT, "up", "down"),
        np.where(pct > frame["prev_pct"], "up", np.where(pct < frame["prev_pct"], "down", "none")),
    )
    frame["direction"] = direction
    frame = frame[(frame["direction"] != "none") & (pd.to_datetime(frame["obligation_date"]) >= pd.Timestamp(start))]
    frame["has_potential"] = (frame["pct_capital_potential"].fillna(0.0) > 0) if "pct_capital_potential" in frame else frame["has_potential"].astype(bool)
    return frame[list(EVENT_COLUMNS)].reset_index(drop=True)


def market_returns(closes: Mapping[str, pd.Series], *, clip: float = RETURN_CLIP) -> pd.Series:
    """The equal-weight daily return of the series in ``closes`` (adjusted closes), clipped per name."""
    frame = pd.DataFrame({k: v for k, v in closes.items()}).sort_index()
    returns = frame.pct_change(fill_method=None).clip(-clip, clip)
    out = returns.mean(axis=1, skipna=True)
    return out.where(returns.notna().sum(axis=1) > 0).dropna()


def _compound(returns: pd.Series, start: pd.Timestamp, end: pd.Timestamp) -> float:
    window = returns.loc[(returns.index > start) & (returns.index <= end)]
    if window.empty:
        return float("nan")
    return float(np.prod(1.0 + window.to_numpy(dtype=float)) - 1.0)


def event_cars(
    events: pd.DataFrame,
    closes: Mapping[str, pd.Series],
    market: pd.Series,
    *,
    horizons: Sequence[int] = HORIZONS,
    pre_window: int = PRE_WINDOW,
    clip: float = RETURN_CLIP,
) -> pd.DataFrame:
    """Every event with its entry close date, the CAR at each horizon and the pre-event CAR.

    ``closes``: issuer name to its adjusted close series; an issuer without a
    series, or an event without ``h`` closes after the entry, carries NaN at
    that horizon (``entry_date`` NaN when the series ends before
    ``published_from``).
    """
    market = market.sort_index()
    out = events.copy()
    out["entry_date"] = pd.NaT
    out["pre_car"] = np.nan
    for h in horizons:
        out[f"car_{h}"] = np.nan
    daily: dict[str, pd.Series] = {}
    for issuer, series in closes.items():
        s = series.sort_index().dropna()
        daily[issuer] = s.pct_change(fill_method=None).clip(-clip, clip)
    for idx, row in out.iterrows():
        rets = daily.get(row["issuer"])
        if rets is None or rets.empty:
            continue
        dates = rets.index
        pos = dates.searchsorted(pd.Timestamp(row["published_from"]), side="left")
        if pos >= len(dates):
            continue
        entry = dates[pos]
        out.at[idx, "entry_date"] = entry
        if pos - pre_window >= 0:
            pre_start = dates[pos - pre_window]
            out.at[idx, "pre_car"] = _compound(rets, pre_start, entry) - _compound(market, pre_start, entry)
        for h in horizons:
            if pos + h < len(dates):
                end = dates[pos + h]
                out.at[idx, f"car_{h}"] = _compound(rets, entry, end) - _compound(market, entry, end)
    out["entry_date"] = pd.to_datetime(out["entry_date"])
    iso = pd.to_datetime(out["published_from"]).dt.isocalendar()
    out["cluster"] = iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)
    return out


def cluster_t(values: pd.Series, clusters: pd.Series) -> tuple[float, float, int, int]:
    """Mean, t with a standard error clustered on ``clusters``, the count and the cluster count (NaNs dropped)."""
    frame = pd.DataFrame({"v": values, "c": clusters}).dropna()
    n = len(frame)
    if n < 3:
        return float("nan"), float("nan"), n, int(frame["c"].nunique())
    mean = float(frame["v"].mean())
    resid = frame["v"] - mean
    sums = resid.groupby(frame["c"]).sum()
    g = len(sums)
    if g < 2:
        return mean, float("nan"), n, g
    var = float((sums**2).sum()) / (n * n) * g / (g - 1)
    se = math.sqrt(max(var, 1e-18))
    return mean, mean / se, n, g


@dataclass(frozen=True)
class EventResult:
    subset: str
    direction: str
    horizon: int
    n: int
    clusters: int
    mean_car: float
    t_stat: float
    p_value: float
    expected_sign: float
    family_size: int = FAMILY_SIZE
    readable: bool = True

    @property
    def significant(self) -> bool:
        return self.readable and self.p_value < 0.05 / self.family_size

    @property
    def sign_agrees(self) -> bool:
        return math.isfinite(self.mean_car) and math.copysign(1.0, self.mean_car) == self.expected_sign


def run_events(
    cars: pd.DataFrame,
    *,
    subset: str = "hedge_fund",
    horizons: Sequence[int] = (HORIZON,),
    family_size: int = FAMILY_SIZE,
    min_events: int = MIN_EVENTS,
) -> list[EventResult]:
    """The up and down tests at each horizon on an event frame from :func:`event_cars`."""
    results = []
    for direction in ("up", "down"):
        sub = cars[cars["direction"] == direction]
        for h in horizons:
            mean, t, n, g = cluster_t(sub[f"car_{h}"], sub["cluster"])
            results.append(
                EventResult(
                    subset=subset, direction=direction, horizon=h, n=n, clusters=g, mean_car=mean, t_stat=t,
                    p_value=ev.p_value(t, n), expected_sign=ORIENTATION[direction], family_size=family_size, readable=n >= min_events,
                )
            )
    return results


def results_table(results: Iterable[EventResult]) -> pd.DataFrame:
    rows = [
        {
            "subset": r.subset,
            "direction": r.direction,
            "horizon": r.horizon,
            "events": r.n,
            "weeks": r.clusters,
            "mean_car_pct": round(100.0 * r.mean_car, 2) if math.isfinite(r.mean_car) else float("nan"),
            "t": round(r.t_stat, 2) if math.isfinite(r.t_stat) else float("nan"),
            "p": r.p_value,
            "sign_as_spec": r.sign_agrees,
            "readable": r.readable,
            "significant": r.significant,
        }
        for r in results
    ]
    return pd.DataFrame(rows)


def halves(cars: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The events split at the median ``published_from`` (the first half strictly before it)."""
    dates = np.sort(pd.to_datetime(cars["published_from"]).unique())
    if len(dates) == 0:
        return cars.iloc[0:0], cars.iloc[0:0]
    cut = dates[len(dates) // 2]
    when = pd.to_datetime(cars["published_from"])
    return cars[when < cut].copy(), cars[when >= cut].copy()


def ledger(cars: pd.DataFrame, *, start: date = LEDGER_START) -> pd.DataFrame:
    """The out-of-sample events: ``published_from`` after ``start``."""
    return cars[pd.to_datetime(cars["published_from"]) > pd.Timestamp(start)].copy()
