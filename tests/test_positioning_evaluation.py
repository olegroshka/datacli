"""positioning.evaluation: the pre-registered statistics on synthetic data."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import evaluation as ev  # noqa: E402


def test_ewm_zscore_is_causal_and_needs_history() -> None:
    s = pd.Series([1.0] * 10 + [5.0])
    z = ev.ewm_zscore(s)
    assert z.iloc[: ev.MIN_HISTORY].isna().all()
    assert z.iloc[6:10].abs().max() < 1e-3  # flat history: z near zero (sigma floor)
    assert z.iloc[10] > 100  # the jump is scored against the past only
    # the past does not change when the future arrives
    z_prefix = ev.ewm_zscore(s.iloc[:8])
    pd.testing.assert_series_equal(z.iloc[:8], z_prefix)


def test_residualise_removes_a_linear_factor_per_date() -> None:
    rng = np.random.default_rng(1)
    rows = []
    for d in range(3):
        f = rng.normal(size=300)
        rows.append(pd.DataFrame({"date": d, "feature": 2.0 * f + rng.normal(scale=0.1, size=300), "f1": f}))
    frame = pd.concat(rows, ignore_index=True)
    resid = ev.residualise(frame, "feature", ("f1",))
    assert resid.notna().all()
    for _, g in frame.assign(resid=resid).groupby("date"):
        assert abs(np.corrcoef(g["resid"], g["f1"])[0, 1]) < 0.05


def test_daily_ic_and_newey_west_t() -> None:
    rng = np.random.default_rng(2)
    rows = []
    for d in range(40):
        x = rng.normal(size=300)
        rows.append(pd.DataFrame({"date": d, "x": x, "y": 0.3 * x + rng.normal(size=300)}))
    frame = pd.concat(rows, ignore_index=True)
    ics = ev.daily_ic(frame, "x", "y")
    assert len(ics) == 40 and 0.2 < ics.mean() < 0.4
    mean, t = ev.newey_west_t(ics)
    assert mean == pytest.approx(ics.mean()) and t > 10
    assert ev.p_value(t, 40) < 1e-6
    small = frame[frame["date"] == 0].head(50)
    assert ev.daily_ic(small, "x", "y").empty  # below MIN_NAMES


def test_run_family_counts_and_orientation() -> None:
    rng = np.random.default_rng(3)
    dates = pd.date_range("2020-01-15", periods=30, freq="14D")
    rows = []
    for i, d in enumerate(dates):
        n = 300
        z = rng.normal(size=n)
        rows.append(
            pd.DataFrame(
                {
                    "date": d,
                    "symbol": [f"S{k}" for k in range(n)],
                    "level": rng.uniform(size=n),
                    "z_inventory": z,
                    "z_age": rng.normal(size=n),
                    "z_profit": rng.normal(size=n),
                    "flow": rng.normal(size=n),
                    "reversal_21d": rng.normal(size=n),
                    "momentum_12_1": rng.normal(size=n),
                    # heavy accumulation is followed by a lower return: SPEC's sign
                    "ret_adj_10": -0.02 * z + rng.normal(scale=0.05, size=n),
                    "ret_adj_21": -0.02 * z + rng.normal(scale=0.05, size=n),
                }
            )
        )
    frame = pd.concat(rows, ignore_index=True)
    frame["G_short"] = -(frame["z_inventory"] + frame["z_age"] + frame["z_profit"]) / 3
    results = ev.run_family(frame)
    assert len(results) == ev.FAMILY_SIZE == 36
    table = ev.results_table(results)
    hit = table[(table.feature == "z_inventory") & (table.cleaning == "factors+level") & (table.horizon == 10)].iloc[0]
    assert hit.mean_ic < 0 and hit.sign_as_spec and hit.significant
    noise = table[(table.feature == "z_age") & (table.cleaning == "none")]
    assert not noise.significant.any()


def test_add_features_signs_and_flow() -> None:
    dates = pd.date_range("2020-01-15", periods=12, freq="14D")
    frame = pd.DataFrame(
        {
            "symbol": "A",
            "date": dates,
            "inventory": [100.0] * 11 + [200.0],
            "flow": [0.0] * 11 + [100.0],
            "wavg_age_days": list(range(12)),
            "profit_pct": [0.0] * 12,
            "reset": [True] + [False] * 11,
        }
    )
    out = ev.add_features(frame)
    assert out["flow"].iloc[0] != out["flow"].iloc[0]  # NaN on the reset row
    assert out["flow"].iloc[-1] == pytest.approx(1.0)
    assert out["z_inventory"].iloc[-1] > 10 and out["G_short"].iloc[-1] < 0  # accumulation reads bearish
