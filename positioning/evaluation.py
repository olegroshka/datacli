"""Pre-registered evaluation of the short ladder (KB-001 section 5, ADR-001 D12 to D15).

The claim under test: does the *path* of short interest (abnormal
accumulation, how long the short has been held, how far it is in or out of
the money) carry information about forward returns beyond its *level* (short
interest over shares outstanding) and the price factors it correlates with?

Design, fixed before any number was computed:

- Observation: one row per ``(symbol, settlement_date)`` of the stored short
  ladder, lanes ``us_common`` and ``us_extended`` (stocks, not funds), with
  ``seed_share < 0.5`` (the first lot's age is unknown) and inventory > 0.
- Decision date: the report's ``published_at``. Entry at the close of the
  first trading day after it; two exit horizons, 10 and 21 trading days
  after entry. Returns are total (adjusted close) and market-adjusted by the
  same-date cross-sectional median.
- Features at the decision date:
  ``level``       short interest over shares outstanding (the known factor);
  ``z_inventory`` per-symbol EWM z-score of log inventory (accumulation);
  ``z_age``       per-symbol EWM z-score of the weighted age;
  ``z_profit``    per-symbol EWM z-score of the short sellers' log return on cost;
  ``flow``        reported change in inventory over the previous inventory.
  The EWM halflife is 6 observations, about 90 calendar days, SPEC's choice
  restated for twice-monthly data; sigma has a floor.
- SPEC orientation, so every feature is signed to read "+ = bullish":
  heavy or growing short inventory, old shorts and shorts in profit are
  bearish (G4, G6, G7). ``G_short`` is the equal-weight mean of the three
  signed z-scores, SPEC's group mean.
- Cleaning variants: none; residualised per date on 21-day reversal and
  12-1 momentum (``factors``); the same plus ``level`` (``factors+level``,
  the headline per D12). Features are winsorised at 0.5 / 99.5 before OLS.
- Statistic: per-date Spearman rank correlation between the (residual)
  feature and the market-adjusted forward return, over dates with at least
  ``MIN_NAMES`` names; t = mean / standard error over dates, with a
  Newey-West correction (lag 2) because the 21-day horizon overlaps.
- Family: 6 features x 3 cleanings x 2 horizons = 36 tests, Bonferroni at
  5 percent: p < 0.05 / 36. Nothing is a finding below that bar.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np
import pandas as pd

HALFLIFE_OBS = 6
SIGMA_FLOOR = 1e-6
MIN_HISTORY = 6
MIN_NAMES = 200
HORIZONS: tuple[int, ...] = (10, 21)
WINSOR = (0.005, 0.995)
NW_LAG = 2
LANES: tuple[str, ...] = ("us_common", "us_extended")
FEATURES: tuple[str, ...] = ("level", "z_inventory", "z_age", "z_profit", "flow", "G_short")
CLEANINGS: tuple[str, ...] = ("none", "factors", "factors+level")
FAMILY_SIZE = len(FEATURES) * len(CLEANINGS) * len(HORIZONS)
#: SPEC's orientation: the sign of the feature once it reads "+ = bullish".
#: Post-hoc extension (added after the first run showed that ``level`` needs
#: shares outstanding, which only 39 percent of the ``us_extended`` rows have):
#: ``level_dtc`` is days to cover (short interest over average daily volume,
#: published with every report), and ``factors+dtc`` the cleaning on it.
EXTENDED_FEATURES: tuple[str, ...] = (*FEATURES, "level_dtc")
EXTENDED_CLEANINGS: tuple[str, ...] = (*CLEANINGS, "factors+dtc")
EXTENDED_FAMILY_SIZE = len(EXTENDED_FEATURES) * len(EXTENDED_CLEANINGS) * len(HORIZONS)
ORIENTATION = {
    "level": -1.0,
    "level_dtc": -1.0,
    "z_inventory": -1.0,
    "z_age": -1.0,
    "z_profit": -1.0,
    "flow": -1.0,
    "G_short": 1.0,
}


def ewm_zscore(values: pd.Series, *, halflife: int = HALFLIFE_OBS, min_history: int = MIN_HISTORY) -> pd.Series:
    """Causal EWM z-score of one symbol's series: (x - EWM mean) / EWM sigma.

    Uses EWM(x^2) - EWM(x)^2 for the variance (KB-001), a floor on sigma, and
    NaN until ``min_history`` observations precede the row, so the z-score of
    a row never sees its own value or the future.
    """
    x = values.astype(float)
    mean = x.ewm(halflife=halflife, adjust=True).mean().shift(1)
    sq = (x * x).ewm(halflife=halflife, adjust=True).mean().shift(1)
    var = (sq - mean * mean).clip(lower=0.0)
    sigma = np.sqrt(var).clip(lower=SIGMA_FLOOR)
    z = (x - mean) / sigma
    count = x.notna().cumsum().shift(1).fillna(0)
    z[count < min_history] = np.nan
    return z


def winsorise(values: pd.Series, limits: tuple[float, float] = WINSOR) -> pd.Series:
    lo, hi = values.quantile(limits[0]), values.quantile(limits[1])
    return values.clip(lower=lo, upper=hi)


def residualise(frame: pd.DataFrame, feature: str, factors: Sequence[str]) -> pd.Series:
    """Per-date OLS residual of ``feature`` on ``factors`` (with intercept)."""
    out = pd.Series(np.nan, index=frame.index)
    cols = [feature, *factors]
    for _, group in frame.groupby("date", sort=False):
        g = group[cols].dropna()
        if len(g) < MIN_NAMES:
            continue
        y = winsorise(g[feature]).to_numpy(dtype=float)
        x = np.column_stack([np.ones(len(g)), *(winsorise(g[f]).to_numpy(dtype=float) for f in factors)])
        beta, *_ = np.linalg.lstsq(x, y, rcond=None)
        out.loc[g.index] = y - x @ beta
    return out


def daily_ic(frame: pd.DataFrame, feature: str, target: str) -> pd.Series:
    """Spearman correlation per date between ``feature`` and ``target``."""
    ics = {}
    for date, group in frame.groupby("date", sort=True):
        g = group[[feature, target]].dropna()
        if len(g) < MIN_NAMES:
            continue
        ics[date] = g[feature].rank().corr(g[target].rank())
    return pd.Series(ics, dtype=float)


@dataclass(frozen=True)
class TestResult:
    feature: str
    cleaning: str
    horizon: int
    n_dates: int
    mean_ic: float
    t_stat: float
    p_value: float
    expected_sign: float

    family_size: int = FAMILY_SIZE

    @property
    def significant(self) -> bool:
        return self.p_value < 0.05 / self.family_size

    @property
    def sign_agrees(self) -> bool:
        return math.copysign(1.0, self.mean_ic) == self.expected_sign


def newey_west_t(series: pd.Series, lag: int = NW_LAG) -> tuple[float, float]:
    """Mean and t-statistic with a Newey-West (Bartlett) long-run variance."""
    x = series.dropna().to_numpy(dtype=float)
    n = len(x)
    if n < 3:
        return float("nan"), float("nan")
    mean = x.mean()
    e = x - mean
    gamma0 = float(e @ e) / n
    lrv = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        w = 1.0 - k / (lag + 1)
        lrv += 2.0 * w * float(e[k:] @ e[:-k]) / n
    se = math.sqrt(max(lrv, 1e-18) / n)
    return mean, mean / se


def p_value(t: float, n: int) -> float:
    """Two-sided p from a normal approximation (n is large here)."""
    if not math.isfinite(t):
        return float("nan")
    return float(math.erfc(abs(t) / math.sqrt(2.0)))


def add_features(ladder: pd.DataFrame) -> pd.DataFrame:
    """Per-symbol path features on the ladder rows (sorted by symbol, date)."""
    frame = ladder.sort_values(["symbol", "date"]).copy()
    frame["log_inventory"] = np.log(frame["inventory"].clip(lower=1e-9))
    frame["profit_log"] = -np.log1p(-frame["profit_pct"].clip(upper=0.999999))
    prev = frame.groupby("symbol")["inventory"].shift(1)
    same_series = ~frame["reset"].astype(bool)
    frame["flow"] = np.where(same_series & (prev > 0), frame["flow"] / prev, np.nan)
    for name, source in (("z_inventory", "log_inventory"), ("z_age", "wavg_age_days"), ("z_profit", "profit_log")):
        frame[name] = frame.groupby("symbol", sort=False)[source].transform(ewm_zscore)
    # SPEC's group mean over the three signed single-member groups
    signed = pd.concat(
        [ORIENTATION[c] * frame[c] for c in ("z_inventory", "z_age", "z_profit")], axis=1
    )
    frame["G_short"] = signed.mean(axis=1, skipna=False)
    return frame


def run_family(
    frame: pd.DataFrame,
    *,
    horizons: Sequence[int] = HORIZONS,
    features: Sequence[str] = FEATURES,
    cleanings: Sequence[str] = CLEANINGS,
) -> list[TestResult]:
    """Every test of a family on a prepared frame (the pre-registered one by default).

    ``frame`` needs ``date``, ``symbol``, the features, ``reversal_21d``,
    ``momentum_12_1`` and ``ret_adj_<h>`` for each horizon.
    """
    results: list[TestResult] = []
    family_size = len(features) * len(cleanings) * len(horizons)
    factor_sets = {
        "none": (),
        "factors": ("reversal_21d", "momentum_12_1"),
        "factors+level": ("reversal_21d", "momentum_12_1", "level"),
        "factors+dtc": ("reversal_21d", "momentum_12_1", "level_dtc"),
    }
    for feature in features:
        for cleaning in cleanings:
            factors = tuple(f for f in factor_sets[cleaning] if f != feature)
            column = feature
            if factors:
                column = f"_{feature}_{cleaning}"
                frame[column] = residualise(frame, feature, factors)
            for horizon in horizons:
                ics = daily_ic(frame, column, f"ret_adj_{horizon}")
                mean, t = newey_west_t(ics)
                results.append(
                    TestResult(
                        feature=feature,
                        cleaning=cleaning,
                        horizon=horizon,
                        n_dates=int(len(ics)),
                        mean_ic=float(mean),
                        t_stat=float(t),
                        p_value=p_value(t, len(ics)),
                        expected_sign=ORIENTATION[feature],
                        family_size=family_size,
                    )
                )
    return results


def results_table(results: Sequence[TestResult]) -> pd.DataFrame:
    rows = [
        {
            "feature": r.feature,
            "cleaning": r.cleaning,
            "horizon": r.horizon,
            "dates": r.n_dates,
            "mean_ic": round(r.mean_ic, 4),
            "t": round(r.t_stat, 2),
            "p": r.p_value,
            "sign_as_spec": r.sign_agrees,
            "significant": r.significant,
        }
        for r in results
    ]
    return pd.DataFrame(rows)


def load_panel(con: Any, *, horizons: Sequence[int] = HORIZONS) -> pd.DataFrame:
    """The evaluation panel from the datacli views: ladder rows, level, factors, forward returns."""
    lanes = ", ".join(f"'{lane}'" for lane in LANES)
    max_h = max(horizons)
    exits = ", ".join(
        f"x{h}.adjusted_close / e.adjusted_close - 1 AS ret_{h}" for h in horizons
    )
    exit_joins = " ".join(
        f"LEFT JOIN px x{h} ON x{h}.ticker = e.ticker AND x{h}.rn = e.rn + {h}" for h in horizons
    )
    sql = f"""
    WITH ladder AS (
      SELECT eodhd_code AS symbol, settlement_date, published_at, lane, inventory, flow,
             wavg_age_days, profit_pct, seed_share, reset
      FROM positioning_short_ladder
      WHERE lane IN ({lanes}) AND inventory > 0
    ), px AS (
      SELECT ticker, CAST(date AS DATE) AS date, adjusted_close,
             row_number() OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)) AS rn
      FROM prices
      WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
        AND CAST(date AS DATE) >= DATE '2017-06-01'
    ), entry AS (
      SELECT l.symbol, l.settlement_date, e.date AS entry_date, e.rn, e.adjusted_close, e.ticker
      FROM ladder l
      ASOF JOIN px e ON e.ticker = l.symbol AND e.date > l.published_at
    )
    SELECT l.*, e.entry_date, {exits},
           f.reversal_21d, f.momentum_12_1, f.short_interest_ratio AS level,
           si.days_to_cover AS level_dtc
    FROM ladder l
    LEFT JOIN finra_short_interest si
      ON si.eodhd_code = l.symbol AND si.settlement_date = l.settlement_date
     AND si.listed AND si.security_kind = 'common'
    LEFT JOIN entry e ON e.symbol = l.symbol AND e.settlement_date = l.settlement_date
    {exit_joins}
    LEFT JOIN positioning_factors f ON f.ticker = l.symbol AND f.date = l.published_at
    """
    panel = con.execute(sql).df()
    panel["date"] = pd.to_datetime(panel["settlement_date"])
    for h in horizons:
        panel[f"ret_adj_{h}"] = panel[f"ret_{h}"] - panel.groupby("date")[f"ret_{h}"].transform("median")
    return panel
