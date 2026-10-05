"""EVAL-009 (b): the brokers' synthetic books on the AFM long register as a weekly cross-section (DD-006 C5).

A broker's disclosed *potential* capital interest in an issuer (swaps,
contracts for difference, options) is its hedge of clients' synthetic
positions, in aggregate the hedge funds' synthetic long exposure through
that broker. Per issuer and date the state is the sum over brokers of the
potential interest in their latest visible notification (``synthetic_pct``)
and the number of brokers with a positive one (``n_brokers``). The weekly
cross-section (the last trading day of each ISO week, every priced issuer a
row, zero where no broker discloses) carries the level and its four-week
change as features, read positive (SPEC's G1 and G3), against 5-, 10- and
21-day returns adjusted by the cross-section's median, through EVAL-005's
cleanings with size in place of the level; 18 tests. The design is fixed in
EVAL-009 before any return was computed.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from positioning import afm_holdings as ah
from positioning import evaluation as ev
from positioning import evaluation_register as er

#: The broker-dealers whose disclosed potential interest is read as the clients' synthetic book (EVAL-009 shared definitions).
BROKERS = re.compile(
    r"\b(goldman|morgan stanley|jp ?morgan|j p morgan|ubs|barclays|bank of america|merrill|citigroup|hsbc|bnp|societe generale|"
    r"credit suisse|deutsche bank|nomura|natixis|jefferies|macquarie|rbc|royal bank of canada|wells fargo|bank of montreal|scotia|"
    r"mizuho|sumitomo|credit agricole|commerzbank|santander|bbva|unicredit|intesa|ing groep|ing bank|abn amro|rabobank|kbc|nordea|seb|"
    r"danske|dnb)\b"
)
#: A bank's asset-management arm, fund or foundation is not its broker-dealer: left out of the brokers.
NOT_BROKER = re.compile(r"asset management|investment management|investors|invest sicav|forskningsfond|stichting|fonds|fund")
SAMPLE_START = date(2013, 1, 1)
HORIZONS: tuple[int, ...] = (5, 10, 21)
FEATURES: tuple[str, ...] = ("synthetic_pct", "synthetic_chg_4w")
CLEANINGS: tuple[str, ...] = ("none", "factors", "factors+size")
FACTOR_SETS = {
    "none": (),
    "factors": ("reversal_21d", "momentum_12_1"),
    "factors+size": ("reversal_21d", "momentum_12_1", "log_mcap"),
}
FAMILY_SIZE = len(FEATURES) * len(CLEANINGS) * len(HORIZONS)
ORIENTATION = {f: 1.0 for f in FEATURES}
MIN_NAMES = 25
CHANGE_WEEKS = 4
RETURN_CLIP = 0.5
ADV_WINDOW = 63
#: Post-hoc extension (added after run 1 showed the level clearing its bar and the post-hoc
#: decomposition placing the ordering on the hedged-against-unhedged boundary): the log of the
#: 63-day median traded value joins the cleaning, since the names no broker hedges are the
#: illiquid ones. Labelled post-hoc in EVAL-009; not part of the pre-registered family.
EXTENDED_CLEANINGS: tuple[str, ...] = ("factors+size+liquidity",)
FACTOR_SETS["factors+size+liquidity"] = ("reversal_21d", "momentum_12_1", "log_mcap", "log_adv")

PANEL_COLUMNS: tuple[str, ...] = ("date", "issuer", "synthetic_pct", "n_brokers")


def broker_holders(holders: pd.DataFrame) -> set[str]:
    """The ``bank_or_passive`` holders whose name matches :data:`BROKERS`."""
    banks = holders[holders["kind"] == "bank_or_passive"]["holder"]
    return {h for h in banks if BROKERS.search(ah.normalise_name(h)) and not NOT_BROKER.search(ah.normalise_name(h))}


def synthetic_panel(notifications: pd.DataFrame, brokers: set[str], dates: pd.DatetimeIndex, issuers: Sequence[str]) -> pd.DataFrame:
    """Per date (the observation dates) and issuer: the sum of the brokers' latest visible potential interest and the broker count."""
    dates = pd.DatetimeIndex(dates).sort_values()
    rows = notifications[notifications["holder"].isin(brokers) & notifications["issuer"].isin(list(issuers))]
    rows = rows[["issuer", "holder", "published_from", "pct_capital_potential"]].copy()
    rows["published_from"] = pd.to_datetime(rows["published_from"])
    rows["pct_capital_potential"] = rows["pct_capital_potential"].fillna(0.0).astype(float)
    rows = rows.sort_values(["issuer", "holder", "published_from"], kind="stable").drop_duplicates(["issuer", "holder", "published_from"], keep="last")
    pieces = []
    for (issuer, _holder), g in rows.groupby(["issuer", "holder"], sort=False):
        state = pd.Series(g["pct_capital_potential"].to_numpy(), index=pd.DatetimeIndex(g["published_from"]))
        state = state[~state.index.duplicated(keep="last")].reindex(state.index.union(dates)).ffill().reindex(dates).fillna(0.0)
        pieces.append(pd.DataFrame({"date": dates, "issuer": issuer, "pct": state.to_numpy()}))
    grid = pd.MultiIndex.from_product([dates, list(issuers)], names=["date", "issuer"]).to_frame(index=False)
    if not pieces:
        grid["synthetic_pct"] = 0.0
        grid["n_brokers"] = 0
        return grid[list(PANEL_COLUMNS)]
    stacked = pd.concat(pieces, ignore_index=True)
    agg = stacked.groupby(["date", "issuer"], sort=False).agg(synthetic_pct=("pct", "sum"), n_brokers=("pct", lambda s: int((s > 0).sum()))).reset_index()
    out = grid.merge(agg, on=["date", "issuer"], how="left")
    out["synthetic_pct"] = out["synthetic_pct"].fillna(0.0)
    out["n_brokers"] = out["n_brokers"].fillna(0).astype(int)
    return out[list(PANEL_COLUMNS)]


def diagnostic(panel: pd.DataFrame) -> pd.DataFrame:
    """Per date: issuers with a positive synthetic book, brokers disclosing (summed over issuers), the median and 90th percentile among the positive."""
    rows = []
    for day, g in panel.groupby("date", sort=True):
        pos = g[g["synthetic_pct"] > 0]
        rows.append(
            dict(
                date=day,
                issuers=len(g),
                with_book=len(pos),
                broker_lines=int(g["n_brokers"].sum()),
                median_pct=float(pos["synthetic_pct"].median()) if len(pos) else float("nan"),
                p90_pct=float(pos["synthetic_pct"].quantile(0.9)) if len(pos) else float("nan"),
            )
        )
    return pd.DataFrame(rows)


def shares_visible(capital: pd.DataFrame, weekdays: int = ah.VISIBILITY_WEEKDAYS) -> pd.DataFrame:
    """The issued-capital register as ``(issuer, visible_from, shares)``, visible by the same weekday rule."""
    rows = capital[["issuer", "date", "issued_capital"]].dropna().copy()
    rows["visible_from"] = pd.to_datetime(ah.visible_from(rows["date"], weekdays))
    return rows.sort_values(["issuer", "visible_from"], kind="stable")[["issuer", "visible_from", "issued_capital"]]


def observations(
    panel: pd.DataFrame,
    closes: Mapping[str, pd.Series],
    *,
    shares: pd.DataFrame | None = None,
    volumes: Mapping[str, pd.Series] | None = None,
    horizons: Sequence[int] = HORIZONS,
    change_weeks: int = CHANGE_WEEKS,
    clip: float = RETURN_CLIP,
) -> pd.DataFrame:
    """The weekly rows with the features, the factors, the forward returns and their cross-sectional adjustment.

    ``closes``: issuer to its adjusted close series (daily). Forward returns
    are compounded clipped daily returns over ``h`` trading days of the
    issuer's own series; the factors are the 21-day return and the 12-1
    momentum at the observation close; ``log_mcap`` is the log of the
    visible issued capital times the close when ``shares`` is given;
    ``log_adv`` the log of the 63-day median of close times volume when
    ``volumes`` (issuer to its daily volume series) is given.
    """
    frames = []
    for issuer, g in panel.groupby("issuer", sort=False):
        series = closes.get(issuer)
        if series is None or series.dropna().empty:
            continue
        s = series.sort_index().dropna()
        rets = s.pct_change(fill_method=None).clip(-clip, clip)
        level = (1.0 + rets.fillna(0.0)).cumprod()
        adv = None
        if volumes is not None and issuer in volumes:
            traded = (s * volumes[issuer].reindex(s.index)).where(lambda v: v > 0)
            adv = traded.rolling(ADV_WINDOW, min_periods=ADV_WINDOW // 2).median().to_numpy()
        dates = pd.DatetimeIndex(g["date"])
        pos = s.index.searchsorted(dates, side="right") - 1
        close_dates = s.index[np.maximum(pos, 0)]
        keep = (pos >= 0) & ((dates - close_dates) <= pd.Timedelta(days=5))  # the last close within the week
        rows = g[keep].copy()
        if rows.empty:
            continue
        rows["close_date"] = close_dates[keep]
        p = pos[keep]
        lvl = level.to_numpy()
        n = len(lvl)
        for h in horizons:
            ahead = p + h
            rows[f"ret_{h}"] = np.where(ahead < n, lvl[np.minimum(ahead, n - 1)] / lvl[p] - 1.0, np.nan)
        rows["reversal_21d"] = np.where(p - 21 >= 0, lvl[p] / lvl[np.maximum(p - 21, 0)] - 1.0, np.nan)
        rows["momentum_12_1"] = np.where(p - 252 >= 0, lvl[np.maximum(p - 21, 0)] / lvl[np.maximum(p - 252, 0)] - 1.0, np.nan)
        rows["close"] = s.to_numpy()[p]
        rows["log_adv"] = np.log(adv[p]) if adv is not None else np.nan
        rows["synthetic_chg_4w"] = rows["synthetic_pct"] - rows["synthetic_pct"].shift(change_weeks)
        frames.append(rows)
    if not frames:
        return pd.DataFrame()
    out = pd.concat(frames, ignore_index=True)
    out["log_mcap"] = np.nan
    if shares is not None and not shares.empty:
        out = out.sort_values("date", kind="stable")
        out["date"] = pd.to_datetime(out["date"]).astype("datetime64[ns]")
        right = shares.sort_values("visible_from").copy()
        right["visible_from"] = pd.to_datetime(right["visible_from"]).astype("datetime64[ns]")
        merged = pd.merge_asof(out, right, left_on="date", right_on="visible_from", by="issuer", direction="backward")
        out["log_mcap"] = np.log((merged["issued_capital"] * merged["close"]).where(merged["issued_capital"] > 0)).to_numpy()
    for h in horizons:
        out[f"ret_adj_{h}"] = out[f"ret_{h}"] - out.groupby("date")[f"ret_{h}"].transform("median")
    return out.sort_values(["date", "issuer"], kind="stable").reset_index(drop=True)


def run_family(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    features: Sequence[str] = FEATURES,
    cleanings: Sequence[str] = CLEANINGS,
    min_names: int = MIN_NAMES,
) -> list[ev.TestResult]:
    """The 18 pre-registered tests (per-date Spearman IC, Newey-West t with lag ``ceil(h / 5)``)."""
    size = len(features) * len(cleanings) * len(horizons)
    results: list[ev.TestResult] = []
    for feature in features:
        for cleaning in cleanings:
            factors = tuple(f for f in FACTOR_SETS[cleaning] if f != feature)
            column = feature
            if factors:
                column = f"_{feature}_{cleaning}"
                frame[column] = er.residualise(frame, feature, factors, min_names=min_names)
            for h in horizons:
                ics = er.daily_ic(frame, column, f"ret_adj_{h}", min_names=min_names)
                mean, t = ev.newey_west_t(ics, lag=er.nw_lag(h))
                results.append(
                    ev.TestResult(
                        feature=feature, cleaning=cleaning, horizon=h, n_dates=int(len(ics)), mean_ic=float(mean), t_stat=float(t),
                        p_value=ev.p_value(t, len(ics)), expected_sign=ORIENTATION[feature], family_size=size,
                    )
                )
    return results
