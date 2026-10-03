"""Pre-registered evaluation of SPEC's short side with fund identity on a public register (EVAL-004).

The design is fixed in EVAL-004 before any number was computed. It reuses
the statistics of :mod:`positioning.evaluation` (EWM z-score, winsorised
per-date OLS, Spearman IC, Newey-West t) on weekly observations of the
issuer panel of :mod:`positioning.register_panel`.

- Observation: the last trading day of each week on which the issuer has at
  least one visible holder and an adjusted close, up to ``SAMPLE_END`` (the
  UK register froze on 2026-07-09); entry at that close; horizons 5, 10 and
  21 trading days of the issuer's own series; market-adjusted total returns.
- Features: ``level``, ``n_holders``, ``flow_21`` (summed visible change over
  21 days over the level 21 days earlier), ``z_level`` (EWM z-score of log
  level, halflife 63, 63 days of history), ``age_days``, ``profit_pct``.
- Orientation: every feature reads "- = bearish" (SPEC's short side).
- Cleanings none / factors / factors+level; 54 tests; a date needs 50 names.
"""

from __future__ import annotations

import math
from datetime import date
from typing import Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev

HORIZONS: tuple[int, ...] = (5, 10, 21)
FEATURES: tuple[str, ...] = ("level", "n_holders", "flow_21", "z_level", "age_days", "profit_pct")
CLEANINGS: tuple[str, ...] = ("none", "factors", "factors+level")
FAMILY_SIZE = len(FEATURES) * len(CLEANINGS) * len(HORIZONS)
ORIENTATION = {f: -1.0 for f in FEATURES}
MIN_NAMES = 50
MIN_HOLDERS = 1
HALFLIFE_DAYS = 63
MIN_HISTORY_DAYS = 63
FLOW_WINDOW = 21
#: The UK register's last position date (the FCA publishes aggregates since).
SAMPLE_END = date(2026, 7, 9)
FACTOR_SETS = {
    "none": (),
    "factors": ("reversal_21d", "momentum_12_1"),
    "factors+level": ("reversal_21d", "momentum_12_1", "level"),
    "factors+mom6": ("reversal_21d", "momentum_12_1", "momentum_6_1"),
    "factors+mom6+level": ("reversal_21d", "momentum_12_1", "momentum_6_1", "level"),
}
#: Post-hoc extension (added after run 1 showed the funds' short profit surviving the
#: pre-registered cleanings): a short in profit is a stock that fell since the lots
#: opened, which 12-1 momentum and 21-day reversal only partly span, so the six-month
#: return skipping the last month (``momentum_6_1``) is added to the cleaning.
EXTENDED_CLEANINGS: tuple[str, ...] = (*CLEANINGS, "factors+mom6", "factors+mom6+level")
EXTENDED_FAMILY_SIZE = len(FEATURES) * len(EXTENDED_CLEANINGS) * len(HORIZONS)


def nw_lag(horizon: int) -> int:
    """Weekly observations overlap for ``horizon`` trading days: lag ``ceil(horizon / 5)``."""
    return int(math.ceil(horizon / 5))


def add_features(panel: pd.DataFrame, *, horizons: Sequence[int] = HORIZONS) -> pd.DataFrame:
    """Per-issuer derived features and forward returns on the daily panel (sorted by isin, date)."""
    frame = panel.sort_values(["isin", "date"], kind="stable").copy()
    group = frame.groupby("isin", sort=False)
    rolling_flow = group["flow"].transform(lambda s: s.rolling(FLOW_WINDOW, min_periods=FLOW_WINDOW).sum())
    level_before = group["level"].shift(FLOW_WINDOW)
    frame["flow_21"] = (rolling_flow / level_before).where(level_before > 0)
    log_level = np.log(frame["level"].where(frame["level"] > 0))
    frame["_log_level"] = log_level
    frame["z_level"] = frame.groupby("isin", sort=False)["_log_level"].transform(
        lambda s: ev.ewm_zscore(s, halflife=HALFLIFE_DAYS, min_history=MIN_HISTORY_DAYS)
    )
    frame = frame.drop(columns=["_log_level"])
    adjusted = group["adjusted_close"]
    frame["momentum_6_1"] = adjusted.shift(21) / adjusted.shift(126) - 1.0
    for h in horizons:
        ahead = group["adjusted_close"].shift(-h)
        frame[f"ret_{h}"] = ahead / frame["adjusted_close"] - 1.0
    return frame


def week_ends(dates: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The last date of each ISO week present in ``dates``."""
    idx = pd.DatetimeIndex(dates).sort_values().unique()
    key = idx.isocalendar()
    frame = pd.DataFrame({"date": idx, "year": key["year"].to_numpy(), "week": key["week"].to_numpy()})
    return pd.DatetimeIndex(frame.groupby(["year", "week"], sort=False)["date"].max().to_numpy())


def observations(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    sample_end: date = SAMPLE_END,
    min_holders: int = MIN_HOLDERS,
) -> pd.DataFrame:
    """Weekly rows with a visible holder and an adjusted close, market-adjusted returns added."""
    ends = week_ends(pd.DatetimeIndex(frame["date"].unique()))
    rows = frame[frame["date"].isin(ends) & (frame["n_holders"] >= min_holders) & frame["adjusted_close"].notna()]
    rows = rows[rows["date"] <= pd.Timestamp(sample_end)].copy()
    for h in horizons:
        rows[f"ret_adj_{h}"] = rows[f"ret_{h}"] - rows.groupby("date")[f"ret_{h}"].transform("median")
    return rows


def halves(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The rows split at the median observation date (first half strictly before it)."""
    dates = np.sort(frame["date"].unique())
    cut = dates[len(dates) // 2]
    return frame[frame["date"] < cut].copy(), frame[frame["date"] >= cut].copy()


def residualise(frame: pd.DataFrame, feature: str, factors: Sequence[str], *, min_names: int = MIN_NAMES) -> pd.Series:
    """Per-date winsorised OLS residual, as :func:`positioning.evaluation.residualise` with a smaller cross-section."""
    out = pd.Series(np.nan, index=frame.index)
    cols = [feature, *factors]
    for _, group in frame.groupby("date", sort=False):
        g = group[cols].dropna()
        if len(g) < min_names:
            continue
        y = ev.winsorise(g[feature]).to_numpy(dtype=float)
        x = np.column_stack([np.ones(len(g)), *(ev.winsorise(g[f]).to_numpy(dtype=float) for f in factors)])
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        out.loc[g.index] = y - x @ beta
    return out


def daily_ic(frame: pd.DataFrame, feature: str, target: str, *, min_names: int = MIN_NAMES) -> pd.Series:
    ics = {}
    for day, group in frame.groupby("date", sort=True):
        g = group[[feature, target]].dropna()
        if len(g) < min_names:
            continue
        ics[day] = g[feature].rank().corr(g[target].rank())
    return pd.Series(ics, dtype=float)


def run_family(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    features: Sequence[str] = FEATURES,
    cleanings: Sequence[str] = CLEANINGS,
    family_size: int | None = None,
) -> list[ev.TestResult]:
    size = family_size or len(features) * len(cleanings) * len(horizons)
    results: list[ev.TestResult] = []
    for feature in features:
        for cleaning in cleanings:
            factors = tuple(f for f in FACTOR_SETS[cleaning] if f != feature)
            column = feature
            if factors:
                column = f"_{feature}_{cleaning}"
                frame[column] = residualise(frame, feature, factors)
            for horizon in horizons:
                ics = daily_ic(frame, column, f"ret_adj_{horizon}")
                mean, t = ev.newey_west_t(ics, lag=nw_lag(horizon))
                results.append(
                    ev.TestResult(
                        feature=feature, cleaning=cleaning, horizon=horizon, n_dates=int(len(ics)),
                        mean_ic=float(mean), t_stat=float(t), p_value=ev.p_value(t, len(ics)),
                        expected_sign=ORIENTATION[feature], family_size=size,
                    )
                )
    return results
