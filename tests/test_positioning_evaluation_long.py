"""positioning.evaluation_long: EVAL-002's statistics on synthetic quarterly data."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import evaluation_long as evl  # noqa: E402


def test_rank_unit_maps_to_minus_one_one_and_ignores_nan() -> None:
    r = evl.rank_unit(pd.Series([3.0, 1.0, np.nan, 2.0]))
    assert r.tolist()[0] == 1.0 and r.tolist()[1] == -1.0 and r.tolist()[3] == 0.0 and np.isnan(r.tolist()[2])
    assert evl.rank_unit(pd.Series([1.0])).isna().all()


def test_add_features_signs_group_means_and_flow() -> None:
    dates = pd.date_range("2020-03-31", periods=8, freq="QE")
    rows = []
    for s, scale in (("A", 1.0), ("B", 2.0)):
        rows.append(
            pd.DataFrame(
                {
                    "symbol": s,
                    "date": dates,
                    "inventory": [100.0 * scale] * 7 + [200.0 * scale],
                    "flow": [0.0] * 7 + [100.0 * scale],
                    "wavg_age_days": list(range(0, 720, 90)),
                    "profit_pct": [0.0] * 7 + [0.5],
                    "reset": [True] + [False] * 7,
                    "long_fund_weight": [scale] * 8,
                    "long_conc": [1.0 / scale] * 8,
                    "best_ideas": [0.01 * scale] * 8,
                }
            )
        )
    out = evl.add_features(pd.concat(rows, ignore_index=True))
    a = out[out.symbol == "A"]
    assert np.isnan(a["flow"].iloc[0]) and a["flow"].iloc[-1] == pytest.approx(1.0)
    assert a["z_inventory"].iloc[: evl.MIN_HISTORY].isna().all()
    assert a["z_inventory"].iloc[-1] > 10 and a["G_long"].iloc[-1] > 0  # accumulation reads bullish
    assert a["z_profit"].iloc[-1] > 0
    # G1 group mean: B has the larger weight and best ideas but the smaller concentration
    last = out[out.date == dates[-1]].set_index("symbol")
    assert last.loc["B", "G_long_holdings"] == pytest.approx((1 + (-1) + 1) / 3)
    assert last.loc["A", "G_long_holdings"] == pytest.approx((-1 + 1 + (-1)) / 3)


def test_run_family_size_orientation_and_the_two_families() -> None:
    rng = np.random.default_rng(5)
    dates = pd.date_range("2016-03-31", periods=36, freq="QE")
    rows = []
    for d in dates:
        n = 400
        z = rng.normal(size=n)
        w = rng.normal(size=n)
        rows.append(
            pd.DataFrame(
                {
                    "date": d,
                    "symbol": [f"C{k}" for k in range(n)],
                    "level": rng.uniform(size=n),
                    "z_inventory": z,
                    "z_age": rng.normal(size=n),
                    "z_profit": rng.normal(size=n),
                    "flow": rng.normal(size=n),
                    "G_long": z / 3,
                    "long_fund_weight": w,
                    "long_conc": rng.uniform(size=n),
                    "best_ideas": rng.uniform(size=n),
                    "G_long_holdings": w / 3,
                    "reversal_21d": rng.normal(size=n),
                    "momentum_12_1": rng.normal(size=n),
                    # heavy accumulation and a large weight are followed by a higher return: SPEC's sign
                    "ret_adj_21": 0.02 * z + 0.02 * w + rng.normal(scale=0.05, size=n),
                    "ret_adj_63": 0.02 * z + 0.02 * w + rng.normal(scale=0.05, size=n),
                }
            )
        )
    frame = pd.concat(rows, ignore_index=True)
    results = evl.run_family(frame)
    assert len(results) == evl.FAMILY_SIZE == 60
    table = evl.results_table(results) if hasattr(evl, "results_table") else __import__("positioning.evaluation", fromlist=["x"]).results_table(results)
    hit = table[(table.feature == "z_inventory") & (table.cleaning == "factors+level") & (table.horizon == 21)].iloc[0]
    assert hit.mean_ic > 0 and hit.sign_as_spec and hit.significant
    weight = table[(table.feature == "long_fund_weight") & (table.cleaning == "factors") & (table.horizon == 63)].iloc[0]
    assert weight.mean_ic > 0 and weight.significant
    noise = table[(table.feature == "long_conc") & (table.cleaning == "none")]
    assert not noise.significant.any()
    assert set(evl.ORIENTATION.values()) == {1.0}


def test_usable_applies_the_pre_registered_filters() -> None:
    panel = pd.DataFrame(
        {
            "aggregate": ["all", "all", "all", "cohort", "cohort", "cohort"],
            "date": pd.to_datetime(["2024-03-31", "2024-06-30", "2024-09-30", "2025-09-30", "2025-12-31", "2025-06-30"]),
            "seed_share": [0.1, 0.9, 0.1, 0.1, 0.1, 0.1],
            "entry_date": pd.to_datetime(["2024-05-20", "2024-08-20", None, "2025-11-20", "2026-02-20", "2025-08-20"]),
            "price_quality_flag": [False, False, False, False, False, True],
        }
    )
    assert len(evl.usable(panel, "all")) == 1
    cohort = evl.usable(panel, "cohort")
    assert len(cohort) == 1 and cohort["date"].iloc[0] == pd.Timestamp(evl.COHORT_LAST_PERIOD)
