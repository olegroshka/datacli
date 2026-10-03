"""Pre-registered evaluation of the long ladder and the holdings inputs (EVAL-002).

The design is fixed in EVAL-002 before any number was computed; this module
reuses the statistics of :mod:`positioning.evaluation` (EWM z-score, per-date
residualisation, Spearman IC, Newey-West t) on quarterly observations.

- Observation: one row per ``(cusip, period, aggregate)`` of the stored long
  ladder, lanes ``us_common`` and ``us_extended``, inventory > 0,
  ``seed_share < 0.5``, no price-quality flag on the decision date; joined
  by key to the holdings inputs.
- Decision date: the period's ``published_at``; entry at the next close;
  horizons 21 and 63 trading days; market-adjusted total returns.
- Features: ``level`` (the aggregate's shares over shares outstanding, the
  control), ``z_inventory``, ``z_age``, ``z_profit`` (EWM z-scores with a
  4-observation halflife), ``flow``, ``G_long`` (mean of the signed
  z-scores), ``long_fund_weight``, ``long_conc``, ``best_ideas`` and
  ``G_long_holdings`` (mean of the three after per-date rank normalisation).
- Every feature reads "+ = bullish" on the long side (SPEC).
- Cleanings none / factors / factors+level; Newey-West lag 1.
- Families: primary ``all`` (60 tests); secondary ``cohort`` up to
  2025-09-30, labelled underpowered in advance.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev

HALFLIFE_OBS = 4
MIN_HISTORY = 4
HORIZONS: tuple[int, ...] = (21, 63)
NW_LAG = 1
LANES: tuple[str, ...] = ("us_common", "us_extended")
PATH_FEATURES: tuple[str, ...] = ("level", "z_inventory", "z_age", "z_profit", "flow", "G_long")
HOLDINGS_FEATURES: tuple[str, ...] = ("long_fund_weight", "long_conc", "best_ideas", "G_long_holdings")
FEATURES: tuple[str, ...] = (*PATH_FEATURES, *HOLDINGS_FEATURES)
CLEANINGS: tuple[str, ...] = ev.CLEANINGS
FAMILY_SIZE = len(FEATURES) * len(CLEANINGS) * len(HORIZONS)
#: The cohort aggregate's definition changes at 2025-12-31 (DD-001 section 9).
COHORT_LAST_PERIOD = date(2025, 9, 30)
MAX_SEED_SHARE = 0.5
#: SPEC's long-side orientation: every feature reads "+ = bullish".
ORIENTATION = {f: 1.0 for f in FEATURES}
#: Post-hoc extension (added after the first run showed G1's members loading on size:
#: the sum of weights and the best-idea count grow with market capitalisation, and the
#: pre-registered cleanings carry no size factor): ``log_mcap`` is the log market
#: capitalisation at entry (shares outstanding times the entry close), and the two
#: extra cleanings add it.
EXTENDED_CLEANINGS: tuple[str, ...] = (*CLEANINGS, "factors+size", "factors+level+size")
EXTENDED_FAMILY_SIZE = len(FEATURES) * len(EXTENDED_CLEANINGS) * len(HORIZONS)


def rank_unit(values: pd.Series) -> pd.Series:
    """Per-date rank mapped to [-1, 1] (SPEC's ``rank_n_fill`` then rescale)."""
    n = values.notna().sum()
    if n < 2:
        return pd.Series(np.nan, index=values.index)
    return (values.rank() - 1) / (n - 1) * 2.0 - 1.0


def add_features(ladder: pd.DataFrame) -> pd.DataFrame:
    """Per-security path features and the two group means on quarterly rows.

    ``ladder`` carries one aggregate only, sorted by ``symbol`` (the CUSIP),
    ``date`` (the period), with ``inventory``, ``flow``, ``wavg_age_days``,
    ``profit_pct``, ``reset`` and the three holdings inputs.
    """
    frame = ladder.sort_values(["symbol", "date"]).copy()
    frame["log_inventory"] = np.log(frame["inventory"].clip(lower=1e-9))
    frame["profit_log"] = np.log1p(frame["profit_pct"].clip(lower=-0.999999))
    prev = frame.groupby("symbol")["inventory"].shift(1)
    same_series = ~frame["reset"].astype(bool)
    frame["flow"] = np.where(same_series & (prev > 0), frame["flow"] / prev, np.nan)
    for name, source in (("z_inventory", "log_inventory"), ("z_age", "wavg_age_days"), ("z_profit", "profit_log")):
        frame[name] = frame.groupby("symbol", sort=False)[source].transform(
            lambda s: ev.ewm_zscore(s, halflife=HALFLIFE_OBS, min_history=MIN_HISTORY)
        )
    signed = pd.concat([ORIENTATION[c] * frame[c] for c in ("z_inventory", "z_age", "z_profit")], axis=1)
    frame["G_long"] = signed.mean(axis=1, skipna=False)
    ranked = pd.concat(
        [rank_unit_by(frame, "date", c) for c in ("long_fund_weight", "long_conc", "best_ideas")], axis=1
    )
    frame["G_long_holdings"] = ranked.mean(axis=1, skipna=False)
    return frame


def rank_unit_by(frame: pd.DataFrame, key: str, column: str) -> pd.Series:
    """``rank_unit`` within each group of ``key``, vectorised."""
    group = frame.groupby(key, sort=False)[column]
    n = group.transform("count")
    out = (group.rank() - 1) / (n - 1) * 2.0 - 1.0
    return out.where(n >= 2)


def run_family(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    features: Sequence[str] = FEATURES,
    cleanings: Sequence[str] = CLEANINGS,
    orientation: dict[str, float] | None = None,
    family_size: int | None = None,
) -> list[ev.TestResult]:
    """Every test of the family on a prepared frame (one aggregate).

    ``orientation`` defaults to SPEC's long-side reading; ``family_size`` to
    the number of tests run here (EVAL-003 pools aggregates into one family
    and passes the pooled size).
    """
    orientation = ORIENTATION if orientation is None else orientation
    results: list[ev.TestResult] = []
    if family_size is None:
        family_size = len(features) * len(cleanings) * len(horizons)
    factor_sets = {
        "none": (),
        "factors": ("reversal_21d", "momentum_12_1"),
        "factors+level": ("reversal_21d", "momentum_12_1", "level"),
        "factors+size": ("reversal_21d", "momentum_12_1", "log_mcap"),
        "factors+level+size": ("reversal_21d", "momentum_12_1", "level", "log_mcap"),
    }
    for feature in features:
        for cleaning in cleanings:
            factors = tuple(f for f in factor_sets[cleaning] if f != feature)
            column = feature
            if factors:
                column = f"_{feature}_{cleaning}"
                frame[column] = ev.residualise(frame, feature, factors)
            for horizon in horizons:
                ics = ev.daily_ic(frame, column, f"ret_adj_{horizon}")
                mean, t = ev.newey_west_t(ics, lag=NW_LAG)
                results.append(
                    ev.TestResult(
                        feature=feature,
                        cleaning=cleaning,
                        horizon=horizon,
                        n_dates=int(len(ics)),
                        mean_ic=float(mean),
                        t_stat=float(t),
                        p_value=ev.p_value(t, len(ics)),
                        expected_sign=orientation[feature],
                        family_size=family_size,
                    )
                )
    return results


def load_panel(con: Any, *, horizons: Sequence[int] = HORIZONS) -> pd.DataFrame:
    """The evaluation panel from the datacli views, both aggregates in one frame."""
    lanes = ", ".join(f"'{lane}'" for lane in LANES)
    exits = ", ".join(f"x{h}.adjusted_close / e.adjusted_close - 1 AS ret_{h}" for h in horizons)
    exit_joins = " ".join(
        f"LEFT JOIN px x{h} ON x{h}.ticker = e.ticker AND x{h}.rn = e.rn + {h}" for h in horizons
    )
    sql = f"""
    WITH ladder AS (
      SELECT cusip AS symbol, period, published_at, aggregate, eodhd_code, lane, inventory, flow,
             wavg_age_days, profit_pct, seed_share, reset, level_raw, n_managers
      FROM positioning_long_ladder
      WHERE lane IN ({lanes}) AND inventory > 0
    ), px AS (
      SELECT ticker, CAST(date AS DATE) AS date, adjusted_close, close AS raw_close,
             row_number() OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)) AS rn
      FROM prices
      WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
        AND CAST(date AS DATE) >= DATE '2013-01-01'
    ), entry AS (
      SELECT l.symbol, l.period, l.aggregate, e.date AS entry_date, e.rn, e.adjusted_close, e.raw_close, e.ticker
      FROM ladder l
      ASOF JOIN px e ON e.ticker = l.eodhd_code AND e.date > l.published_at
    ), so AS (
      SELECT eodhd_code, published_at, shares_outstanding FROM finra_short_interest_float
      WHERE shares_outstanding > 0
    )
    SELECT l.*, e.entry_date, {exits},
           f.reversal_21d, f.momentum_12_1, coalesce(f.price_quality_flag, false) AS price_quality_flag,
           CASE WHEN s.shares_outstanding > 0 THEN l.level_raw / s.shares_outstanding END AS level,
           CASE WHEN s.shares_outstanding > 0 AND e.raw_close > 0 THEN ln(s.shares_outstanding * e.raw_close) END AS log_mcap,
           h.long_fund_weight, h.long_conc, h.best_ideas, h.n_holders
    FROM ladder l
    LEFT JOIN entry e ON e.symbol = l.symbol AND e.period = l.period AND e.aggregate = l.aggregate
    {exit_joins}
    LEFT JOIN positioning_factors f ON f.ticker = l.eodhd_code AND f.date = l.published_at
    ASOF LEFT JOIN so s ON s.eodhd_code = l.eodhd_code AND s.published_at < l.published_at
    LEFT JOIN positioning_holdings_inputs h
      ON h.cusip = l.symbol AND h.period = l.period AND h.aggregate = l.aggregate
    """
    panel = con.execute(sql).df()
    panel["date"] = pd.to_datetime(panel["period"])
    for h in horizons:
        panel[f"ret_adj_{h}"] = panel[f"ret_{h}"] - panel.groupby(["aggregate", "date"])[f"ret_{h}"].transform("median")
    return panel


def usable(
    panel: pd.DataFrame,
    aggregate: str,
    *,
    max_seed_share: float = MAX_SEED_SHARE,
    cohort_last_period: date | None = COHORT_LAST_PERIOD,
) -> pd.DataFrame:
    """The pre-registered filters for one aggregate.

    EVAL-002 cuts the cohort at ``COHORT_LAST_PERIOD``; EVAL-003 passes
    ``None`` and handles the break on the flow feature instead.
    """
    frame = panel[panel["aggregate"] == aggregate]
    frame = frame[(frame["seed_share"] < max_seed_share) & frame["entry_date"].notna()]
    frame = frame[~frame["price_quality_flag"].fillna(False).astype(bool)]
    if aggregate == "cohort" and cohort_last_period is not None:
        frame = frame[frame["date"] <= pd.Timestamp(cohort_last_period)]
    return frame.copy()
