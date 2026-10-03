"""positioning.evaluation_ownership: EVAL-003's family, flow rule, gates and ledger on synthetic data."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import evaluation as ev  # noqa: E402
from positioning import evaluation_long as evl  # noqa: E402
from positioning import evaluation_ownership as evo  # noqa: E402


def test_family_sizes_and_orientation_are_the_pre_registered_ones() -> None:
    assert evo.PRIMARY_SIZE == 12 and evo.SECONDARY_SIZE == 8
    assert evo.ORIENTATION == {"level": 1.0, "flow": 1.0, "long_fund_weight": -1.0, "best_ideas": -1.0}
    assert evo.OOS_FIRST_PERIOD == date(2026, 9, 30) and evo.COHORT_TRANSITION_PERIODS == (date(2025, 12, 31), date(2026, 3, 31))


def test_flow_ratio_requires_the_preceding_quarter_and_skips_resets_and_transitions() -> None:
    frame = pd.DataFrame(
        {
            "symbol": ["A"] * 5 + ["B"] * 2,
            "date": pd.to_datetime(
                ["2025-06-30", "2025-09-30", "2025-12-31", "2026-06-30", "2026-09-30", "2025-09-30", "2025-12-31"]
            ),
            "inventory": [100.0, 110.0, 120.0, 130.0, 140.0, 50.0, 60.0],
            "flow": [np.nan, 10.0, 10.0, 10.0, 10.0, np.nan, 10.0],
            "reset": [True, False, False, False, True, True, False],
        }
    )
    plain = evo.flow_ratio(frame)
    assert np.isnan(plain.iloc[0])  # first row, no previous
    assert plain.iloc[1] == pytest.approx(0.1)
    assert plain.iloc[2] == pytest.approx(10 / 110)
    assert np.isnan(plain.iloc[3])  # 2026-03-31 missing: the previous row is two quarters back
    assert np.isnan(plain.iloc[4])  # reset
    assert plain.iloc[6] == pytest.approx(0.2)
    cohort = evo.flow_ratio(frame, transition=evo.COHORT_TRANSITION_PERIODS)
    assert np.isnan(cohort.iloc[2]) and np.isnan(cohort.iloc[6]) and cohort.iloc[1] == pytest.approx(0.1)
    # the result follows the caller's index order, not the sorted one
    shuffled = frame.iloc[::-1]
    assert evo.flow_ratio(shuffled).tolist()[::-1][1] == pytest.approx(0.1)


def test_prepare_keeps_the_cohort_past_the_break_and_renames_the_ladder_flow() -> None:
    panel = pd.DataFrame(
        {
            "aggregate": ["cohort"] * 3 + ["all"],
            "symbol": ["A"] * 4,
            "date": pd.to_datetime(["2025-09-30", "2025-12-31", "2026-06-30", "2025-09-30"]),
            "inventory": [100.0, 110.0, 120.0, 10.0],
            "flow": [np.nan, 10.0, 5.0, np.nan],
            "reset": [True, False, False, True],
            "seed_share": [0.1] * 4,
            "entry_date": pd.to_datetime(["2025-11-20", "2026-02-20", "2026-08-20", "2025-11-20"]),
            "price_quality_flag": [False] * 4,
        }
    )
    out = evo.prepare(panel, "cohort")
    assert len(out) == 3 and out["flow_shares"].tolist()[1] == 10.0
    assert np.isnan(out["flow"].iloc[1])  # the transition period
    assert np.isnan(out["flow"].iloc[2])  # 2026-03-31 is not in the panel: no preceding quarter
    assert len(evo.prepare(panel, "all")) == 1


def _synthetic(seed: int = 7, n_dates: int = 36, n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.date_range("2016-03-31", periods=n_dates, freq="QE")
    rows = []
    for d in dates:
        size = rng.normal(size=n)
        level = 0.5 * size + rng.normal(size=n)  # ownership share grows with size
        flow = rng.normal(size=n)
        weight = 0.7 * size + rng.normal(size=n)
        noise = rng.normal(scale=0.05, size=n)
        # given size, ownership and flow are followed by higher returns, weight by lower
        ret = 0.02 * (level - 0.5 * size) + 0.02 * flow - 0.02 * (weight - 0.7 * size) + noise
        rows.append(
            pd.DataFrame(
                {
                    "date": d,
                    "symbol": [f"C{k}" for k in range(n)],
                    "level": level,
                    "flow": flow,
                    "log_mcap": size,
                    "long_fund_weight": weight,
                    "best_ideas": rng.uniform(size=n),
                    "reversal_21d": rng.normal(size=n),
                    "momentum_12_1": rng.normal(size=n),
                    "ret_adj_21": ret,
                    "ret_adj_63": ret,
                }
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_run_spec_pools_the_family_size_and_reads_crowding_with_its_sign() -> None:
    frame = _synthetic()
    primary = evo.run_spec(frame, evo.PRIMARY, size=evo.PRIMARY_SIZE)
    assert len(primary) == 6 and {r.family_size for r in primary} == {12}
    table = ev.results_table(primary)
    level = table[(table.feature == "level") & (table.cleaning == "factors+size") & (table.horizon == 63)].iloc[0]
    assert level.mean_ic > 0 and level.sign_as_spec and level.significant
    flow = table[(table.feature == "flow") & (table.cleaning == "factors+level+size") & (table.horizon == 21)].iloc[0]
    assert flow.mean_ic > 0 and flow.significant
    secondary = evo.run_spec(frame, evo.SECONDARY, size=evo.SECONDARY_SIZE)
    assert len(secondary) == 4 and {r.family_size for r in secondary} == {8}
    weight = [r for r in secondary if r.feature == "long_fund_weight" and r.horizon == 63][0]
    assert weight.mean_ic < 0 and weight.sign_agrees and weight.significant
    ideas = [r for r in secondary if r.feature == "best_ideas"]
    assert not any(r.significant for r in ideas)


def test_halves_terciles_and_the_ledger() -> None:
    frame = _synthetic(n_dates=8)
    first, second = evo.halves(frame)
    assert first["date"].nunique() == 4 and second["date"].nunique() == 4
    assert first["date"].max() < second["date"].min()
    terciles = evo.size_tercile(frame)
    assert set(terciles.dropna().unique()) == {0, 1, 2}
    one_date = frame[frame["date"] == frame["date"].iloc[0]].copy()
    one_date["tercile"] = terciles.loc[one_date.index]
    assert one_date.groupby("tercile")["log_mcap"].mean().is_monotonic_increasing
    table = evo.tercile_table(frame)
    assert len(table) == 6 and set(table["tercile"]) == {0, 1, 2} and (table["horizon"] == 63).all()
    ledger = evo.out_of_sample(frame, first_period=date(2017, 6, 30))
    assert ledger["date"].min() == pd.Timestamp("2017-06-30") and ledger["date"].nunique() == 3
    assert not evo.ledger_ready(ledger, horizon=63)
    assert evo.ledger_ready(evo.out_of_sample(frame, first_period=date(2016, 12, 31)), horizon=63)
    missing = evo.out_of_sample(frame, first_period=date(2016, 12, 31)).copy()
    missing["ret_adj_63"] = np.nan
    assert not evo.ledger_ready(missing, horizon=63)


def test_evaluation_long_keeps_its_defaults() -> None:
    frame = _synthetic(n_dates=6)
    for col in ("z_inventory", "z_age", "z_profit", "G_long", "long_conc", "G_long_holdings"):
        frame[col] = np.random.default_rng(1).normal(size=len(frame))
    results = evl.run_family(frame)
    assert len(results) == evl.FAMILY_SIZE and {r.family_size for r in results} == {60}
    assert all(r.expected_sign == 1.0 for r in results)
    panel = pd.DataFrame(
        {
            "aggregate": ["cohort", "cohort"],
            "date": pd.to_datetime(["2025-09-30", "2025-12-31"]),
            "seed_share": [0.1, 0.1],
            "entry_date": pd.to_datetime(["2025-11-20", "2026-02-20"]),
            "price_quality_flag": [False, False],
        }
    )
    assert len(evl.usable(panel, "cohort")) == 1 and len(evl.usable(panel, "cohort", cohort_last_period=None)) == 2
