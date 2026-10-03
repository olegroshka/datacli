"""Pre-registered evaluation of ownership share and flow given size (EVAL-003).

EVAL-002's post-hoc extension found institutional ownership share and the
quarter's net buying by continuing managers predictive once log market
capitalisation is in the cleaning. EVAL-003 fixes that as a narrow family on
the same panel (a replication on seen data), adds two robustness gates and
opens an out-of-sample ledger from the first period no run has seen.

- Panel, filters, decision date, returns and statistic: EVAL-002
  (:mod:`positioning.evaluation_long`), except that the cohort is not cut
  at 2025-09-30: its ``flow`` is missing at the two transition periods
  where the ADV hedge-fund flag reshaped the membership (DD-001 section 9),
  and ``level`` keeps every period.
- ``flow`` is stricter than EVAL-002's: the previous observation must be
  the preceding quarter (at most 100 days earlier) and not a reset.
- Primary family: ``level | factors+size`` and ``flow | factors+size,
  factors+level+size`` at 21 and 63 days on both aggregates, 12 tests.
- Secondary family: ``long_fund_weight`` and ``best_ideas`` given
  ``factors+level+size``, orientation "-" (crowding), 8 tests.
- Gates without a bar: split halves by date; size terciles.
- Out-of-sample ledger: periods from ``OOS_FIRST_PERIOD``, reported once
  it holds ``OOS_MIN_DATES`` usable dates.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Sequence

import numpy as np
import pandas as pd

from positioning import evaluation as ev
from positioning import evaluation_long as evl

HORIZONS: tuple[int, ...] = evl.HORIZONS
AGGREGATES: tuple[str, ...] = ("all", "cohort")
#: The previous observation counts as the preceding quarter within this gap.
MAX_QUARTER_GAP_DAYS = 100
#: Cohort periods where the ADV hedge-fund flag reshaped the membership (DD-001 sections 9 and 12).
COHORT_TRANSITION_PERIODS: tuple[date, ...] = (date(2025, 12, 31), date(2026, 3, 31))
#: First period whose returns no run had seen when EVAL-003 was pre-registered.
OOS_FIRST_PERIOD = date(2026, 9, 30)
OOS_MIN_DATES = 4
N_TERCILES = 3

PRIMARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("level", ("factors+size",)),
    ("flow", ("factors+size", "factors+level+size")),
)
SECONDARY: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("long_fund_weight", ("factors+level+size",)),
    ("best_ideas", ("factors+level+size",)),
)
ORIENTATION: dict[str, float] = {"level": 1.0, "flow": 1.0, "long_fund_weight": -1.0, "best_ideas": -1.0}
HEADLINE: tuple[tuple[str, str], ...] = (("level", "factors+size"), ("flow", "factors+level+size"))
HEADLINE_HORIZON = 63


def family_size(spec: Sequence[tuple[str, Sequence[str]]], *, aggregates: int = len(AGGREGATES)) -> int:
    return sum(len(cleanings) for _, cleanings in spec) * len(HORIZONS) * aggregates


PRIMARY_SIZE = family_size(PRIMARY)
SECONDARY_SIZE = family_size(SECONDARY)


def flow_ratio(frame: pd.DataFrame, *, transition: Sequence[date] = ()) -> pd.Series:
    """``flow`` over the preceding quarter's inventory, strict about what "preceding" means.

    Missing where the previous row of the security is not the preceding
    quarter, where the row is a reset, where the previous inventory is not
    positive, and at the ``transition`` periods. Computed before the usable
    filters so that the previous row is the previous period where it exists.
    """
    ordered = frame.sort_values(["symbol", "date"])
    group = ordered.groupby("symbol", sort=False)
    prev_inv = group["inventory"].shift(1)
    prev_date = group["date"].shift(1)
    gap_days = (ordered["date"] - prev_date).dt.days
    ok = (~ordered["reset"].astype(bool)) & (prev_inv > 0) & (gap_days <= MAX_QUARTER_GAP_DAYS)
    if transition:
        ok &= ~ordered["date"].isin([pd.Timestamp(d) for d in transition])
    ratio = (ordered["flow"] / prev_inv).where(ok)
    return ratio.reindex(frame.index)


def prepare(panel: pd.DataFrame, aggregate: str) -> pd.DataFrame:
    """One aggregate's usable rows with the EVAL-003 ``flow`` (and the raw ladder flow renamed)."""
    rows = panel[panel["aggregate"] == aggregate].copy()
    rows["flow_shares"] = rows["flow"]
    transition = COHORT_TRANSITION_PERIODS if aggregate == "cohort" else ()
    rows["flow"] = flow_ratio(rows, transition=transition)
    return evl.usable(rows, aggregate, cohort_last_period=None)


def run_spec(
    frame: pd.DataFrame,
    spec: Sequence[tuple[str, Sequence[str]]],
    *,
    size: int,
    horizons: Sequence[int] = HORIZONS,
) -> list[ev.TestResult]:
    """The tests of ``spec`` on one prepared frame, with the pooled family size."""
    results: list[ev.TestResult] = []
    for feature, cleanings in spec:
        results += evl.run_family(
            frame, horizons=horizons, features=(feature,), cleanings=cleanings, orientation=ORIENTATION, family_size=size
        )
    return results


def halves(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The rows split at the median usable date (first half strictly before it)."""
    dates = np.sort(frame["date"].unique())
    cut = dates[len(dates) // 2]
    return frame[frame["date"] < cut].copy(), frame[frame["date"] >= cut].copy()


def size_tercile(frame: pd.DataFrame, *, n: int = N_TERCILES) -> pd.Series:
    """Per-date tercile of ``log_mcap`` (0 = smallest), missing without a size."""
    def _cut(values: pd.Series) -> pd.Series:
        if values.notna().sum() < n:
            return pd.Series(np.nan, index=values.index)
        return pd.qcut(values.rank(method="first"), n, labels=False)

    return frame.groupby("date", sort=False)["log_mcap"].transform(_cut)


def tercile_table(frame: pd.DataFrame, *, horizon: int = HEADLINE_HORIZON) -> pd.DataFrame:
    """Mean IC and t of the headline residual features within each size tercile.

    The residual is taken on the whole cross-section (as in the family) and
    the IC on the tercile's names; no bar applies.
    """
    frame = frame.copy()
    frame["tercile"] = size_tercile(frame)
    rows = []
    for feature, cleaning in HEADLINE:
        column = f"_{feature}_{cleaning}"
        if column not in frame:
            factors = {"factors+size": ("reversal_21d", "momentum_12_1", "log_mcap"),
                       "factors+level+size": ("reversal_21d", "momentum_12_1", "level", "log_mcap")}[cleaning]
            frame[column] = ev.residualise(frame, feature, tuple(f for f in factors if f != feature))
        for tercile, part in frame.groupby("tercile", sort=True):
            ics = ev.daily_ic(part, column, f"ret_adj_{horizon}")
            mean, t = ev.newey_west_t(ics, lag=evl.NW_LAG)
            rows.append({"feature": feature, "cleaning": cleaning, "horizon": horizon, "tercile": int(tercile),
                         "dates": int(len(ics)), "mean_ic": round(float(mean), 4), "t": round(float(t), 2)})
    return pd.DataFrame(rows)


def out_of_sample(frame: pd.DataFrame, *, first_period: date = OOS_FIRST_PERIOD) -> pd.DataFrame:
    """The ledger: rows of periods at or after ``first_period``."""
    return frame[frame["date"] >= pd.Timestamp(first_period)].copy()


def ledger_ready(frame: pd.DataFrame, *, horizon: int, min_dates: int = OOS_MIN_DATES) -> bool:
    """At least ``min_dates`` ledger dates carry the horizon's return."""
    with_return = frame[frame[f"ret_adj_{horizon}"].notna()]
    return int(with_return["date"].nunique()) >= min_dates


def load_panel(con: Any, *, horizons: Sequence[int] = HORIZONS) -> pd.DataFrame:
    return evl.load_panel(con, horizons=horizons)
