"""The daily short-inventory nowcast and EVAL-001's family on it (DD-004 stage 2, the conditional step of EVAL-007).

Given that the period's short volume explains the next print's change
(EVAL-007's first bar), this module builds, for every symbol-day ``d``:

- ``si_pub``: the published timing, the last print whose publication date
  is on or before ``d`` (what a public reader holds);
- ``si_hat``: the nowcast, ``si_pub`` plus the full-period model's estimate
  of every complete period after the known print (settled, not yet
  published) plus the partial-period model's estimate of the period that
  holds ``d`` (fitted on the partial window's cumulative flow, target the
  period's full change);

then EVAL-001's two path features on each series, daily (the EWM z-score
of the log level with a 63-day halflife, and the ten-day growth), and
EVAL-001's family at daily decision dates: per-date Spearman IC against
the market-adjusted forward return at 10 and 21 days, three cleanings,
Newey-West with the horizon as lag, Bonferroni over the twelve tests of a
timing. The gain of the nowcast is the IC difference at ten days.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev
from positioning import nowcast_register as nr
from positioning import nowcast_short_interest as ns

PARTIAL_EXTRA: tuple[str, ...] = ("days_sofar",)
PARTIAL_FEATURES: tuple[str, ...] = ns.STATE + ns.FLOW + PARTIAL_EXTRA
HALFLIFE_DAYS = 63
MIN_HISTORY_DAYS = 63
FLOW_DAYS = 10
HORIZONS: tuple[int, ...] = (10, 21)
FEATURES: tuple[str, ...] = ("z_inventory", "flow")
CLEANINGS: tuple[str, ...] = ("none", "factors", "factors+level")
TIMINGS: tuple[str, ...] = ("nowcast", "published")
FAMILY_SIZE = len(FEATURES) * len(CLEANINGS) * len(HORIZONS)
EXPECTED_SIGN = -1.0  # heavy or growing short inventory is bearish (SPEC's G4)
ITERATIONS = 300


def partial_flow_features(daily: pd.DataFrame, windows: pd.DataFrame) -> pd.DataFrame:
    """Per daily row inside a window, the flow block over the window so far (same names as the full block) and ``days_sofar``."""
    frame = daily.copy()
    frame["date"] = pd.to_datetime(frame["date"])
    frame["settlement_date"] = ns.assign_periods(frame["date"], windows)
    frame = frame[frame["settlement_date"].notna()].sort_values(["symbol", "settlement_date", "date"]).reset_index(drop=True)
    by = frame.groupby(["symbol", "settlement_date"], sort=False)
    adv = by["adv"].transform("first")
    adv = adv.where(adv > 0)
    sv = by["short_volume"].cumsum()
    tv = by["total_volume"].cumsum()
    exempt = by["short_exempt_volume"].cumsum()
    cvol = by["cvolume"].cumsum()
    days = by.cumcount() + 1
    first_close = by["adjusted_close"].transform("first")
    last5 = by["short_volume"].transform(lambda s: s.rolling(ns.LAST_DAYS, min_periods=1).sum())
    out = frame[["date", "symbol", "settlement_date"]].copy()
    out["days_sofar"] = days.astype(float)
    out["sv_adv"] = sv / adv
    out["tv_adv"] = tv / adv
    out["sr_mean"] = sv / tv.where(tv > 0)
    out["exempt_share"] = exempt / sv.where(sv > 0)
    out["cvol_adv"] = cvol / adv
    out["abn_cvol"] = cvol / (adv * days)
    out["ret_period"] = frame["adjusted_close"] / first_close.where(first_close > 0) - 1.0
    out["sv_last_adv"] = last5 / adv
    return out


def fit_models(full: pd.DataFrame, partial: pd.DataFrame, *, fit_to: str = ns.FIT_TO, iterations: int = ITERATIONS) -> tuple[Any, Any]:
    """The full-period model (state plus flow) and the partial-period model, both boosted, fitted on the prints up to ``fit_to``."""
    cut = pd.Timestamp(fit_to)
    full_fit = full[full["settlement_date"] <= cut]
    full_model = nr.make_model("regression", "boost", iterations=iterations)
    full_model.fit(full_fit[list(ns.FEATURE_SETS["state+flow"])], ns.winsorise(full_fit["y"]).to_numpy())
    partial_fit = partial[partial["settlement_date"] <= cut]
    partial_model = nr.make_model("regression", "boost", iterations=iterations)
    partial_model.fit(partial_fit[list(PARTIAL_FEATURES)], ns.winsorise(partial_fit["y"], reference=full_fit["y"]).to_numpy())
    return full_model, partial_model


def partial_rows(partial_features: pd.DataFrame, full: pd.DataFrame) -> pd.DataFrame:
    """The partial rows joined to their period's state block and target (periods that exist in the assembled frame)."""
    state = full[["symbol", "settlement_date", "y", *ns.STATE]]
    return partial_features.merge(state, on=["symbol", "settlement_date"], how="inner")


def daily_nowcast(
    rows: pd.DataFrame,
    full: pd.DataFrame,
    prints: pd.DataFrame,
    *,
    full_model: Any,
    partial_model: Any,
) -> pd.DataFrame:
    """Per symbol-day ``si_pub`` (the last published print) and ``si_hat`` (plus the unpublished complete periods and the partial one).

    ``rows`` are the partial rows (with the state block), ``full`` the assembled periods, ``prints`` has
    ``symbol, settlement_date, published_at, short_position``.
    """
    periods = full[["symbol", "settlement_date", "adv"]].copy()
    periods["y_full"] = np.asarray(full_model.predict(full[list(ns.FEATURE_SETS["state+flow"])]), dtype=float)
    periods = periods.sort_values(["symbol", "settlement_date"]).reset_index(drop=True)
    periods["cum_full"] = periods.groupby("symbol", sort=False)["y_full"].cumsum()
    out = rows[["symbol", "date", "settlement_date"]].copy()
    out["y_partial"] = np.asarray(partial_model.predict(rows[list(PARTIAL_FEATURES)]), dtype=float)
    # the last print published by d
    known = prints[["symbol", "settlement_date", "published_at", "short_position"]].copy()
    known["published_at"] = pd.to_datetime(known["published_at"])
    known["settlement_date"] = pd.to_datetime(known["settlement_date"])
    known = known.sort_values("published_at").rename(columns={"settlement_date": "known_settlement", "short_position": "si_pub"})
    out = out.sort_values("date")
    out = pd.merge_asof(out, known, left_on="date", right_on="published_at", by="symbol", direction="backward")
    # the complete periods after the known print: cum_full at the period before d's minus cum_full at the known one
    cum = periods[["symbol", "settlement_date", "cum_full"]]
    prev_cum = cum.rename(columns={"settlement_date": "prev_of_current", "cum_full": "cum_prev"})
    known_cum = cum.rename(columns={"settlement_date": "known_settlement", "cum_full": "cum_known"})
    calendar = periods[["symbol", "settlement_date"]].copy()
    calendar["prev_of_current"] = calendar.groupby("symbol", sort=False)["settlement_date"].shift(1)
    out = out.merge(calendar, on=["symbol", "settlement_date"], how="left")
    out = out.merge(prev_cum, on=["symbol", "prev_of_current"], how="left").merge(known_cum, on=["symbol", "known_settlement"], how="left")
    adv = periods[["symbol", "settlement_date", "adv"]]
    out = out.merge(adv, on=["symbol", "settlement_date"], how="left")
    between = (out["cum_prev"].fillna(0.0) - out["cum_known"].fillna(0.0)).where(out["known_settlement"] < out["settlement_date"], 0.0)
    out["si_hat"] = out["si_pub"] + (between + out["y_partial"]) * out["adv"]
    out = out[out["si_pub"].notna()].sort_values(["symbol", "date"]).reset_index(drop=True)
    return out[["symbol", "date", "settlement_date", "known_settlement", "adv", "si_pub", "si_hat"]]


def path_features(series: pd.DataFrame) -> pd.DataFrame:
    """EVAL-001's two path features on each timing: ``z_inventory_<timing>`` and ``flow_<timing>``."""
    frame = series.sort_values(["symbol", "date"]).reset_index(drop=True).copy()
    for timing, column in (("nowcast", "si_hat"), ("published", "si_pub")):
        level = frame[column].astype(float).clip(lower=1.0)
        log_level = np.log(level)
        frame[f"z_inventory_{timing}"] = log_level.groupby(frame["symbol"]).transform(
            lambda s: ev.ewm_zscore(s, halflife=HALFLIFE_DAYS, min_history=MIN_HISTORY_DAYS)
        )
        frame[f"flow_{timing}"] = level / level.groupby(frame["symbol"]).shift(FLOW_DAYS) - 1.0
    return frame


@dataclass(frozen=True)
class FamilyResult:
    timing: str
    feature: str
    cleaning: str
    horizon: int
    n_dates: int
    mean_ic: float
    t_stat: float
    p_value: float
    significant: bool
    sign_as_spec: bool


def family(frame: pd.DataFrame, *, min_names: int = ev.MIN_NAMES) -> pd.DataFrame:
    """The twelve tests per timing on a frame with the features, ``reversal_21d``, ``momentum_12_1``, ``level`` and ``ret_adj_<h>``."""
    results: list[FamilyResult] = []
    for timing in TIMINGS:
        for feature in FEATURES:
            column = f"{feature}_{timing}"
            for cleaning in CLEANINGS:
                if cleaning == "none":
                    values = frame[column]
                else:
                    factors = ["reversal_21d", "momentum_12_1"] + (["level"] if cleaning == "factors+level" else [])
                    values = ev.residualise(frame.assign(_f=frame[column]), "_f", factors)
                part = frame[["date"]].assign(_x=values)
                for horizon in HORIZONS:
                    part["_r"] = frame[f"ret_adj_{horizon}"]
                    ics = ev.daily_ic(part, "_x", "_r") if min_names == ev.MIN_NAMES else _ic(part, min_names)
                    mean, t = ev.newey_west_t(ics, lag=horizon)
                    p = ev.p_value(t, len(ics))
                    results.append(FamilyResult(timing, feature, cleaning, horizon, len(ics), mean, t, p, p < 0.05 / FAMILY_SIZE,
                                                math.copysign(1.0, mean) == EXPECTED_SIGN if math.isfinite(mean) else False))
    return pd.DataFrame([r.__dict__ for r in results])


def _ic(frame: pd.DataFrame, min_names: int) -> pd.Series:
    ics = {}
    for date, group in frame.groupby("date", sort=True):
        g = group[["_x", "_r"]].dropna()
        if len(g) < min_names:
            continue
        ics[date] = g["_x"].rank().corr(g["_r"].rank())
    return pd.Series(ics, dtype=float)


def gains(table: pd.DataFrame) -> pd.DataFrame:
    """The nowcast's mean IC minus the published timing's, per feature, cleaning and horizon."""
    wide = table.pivot_table(index=["feature", "cleaning", "horizon"], columns="timing", values="mean_ic", aggfunc="first")
    wide["gain"] = wide["nowcast"] - wide["published"]
    return wide.reset_index()
