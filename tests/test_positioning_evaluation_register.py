"""positioning.evaluation_register: EVAL-004's features, sampling and family on synthetic panels."""

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
from positioning import evaluation_register as er  # noqa: E402


def test_family_constants() -> None:
    assert er.FAMILY_SIZE == 54 and set(er.ORIENTATION.values()) == {-1.0}
    assert er.nw_lag(5) == 1 and er.nw_lag(10) == 2 and er.nw_lag(21) == 5
    assert er.SAMPLE_END == date(2026, 7, 9)


def test_week_ends_and_observations_cut_at_the_sample_end() -> None:
    dates = pd.bdate_range("2026-06-22", "2026-07-17")
    ends = er.week_ends(dates)
    assert [d.strftime("%a %Y-%m-%d") for d in ends] == ["Fri 2026-06-26", "Fri 2026-07-03", "Fri 2026-07-10", "Fri 2026-07-17"]
    assert er.week_ends(dates.drop(pd.Timestamp("2026-06-26")))[0] == pd.Timestamp("2026-06-25")
    panel = pd.DataFrame(
        {
            "isin": "GB1", "date": dates, "n_holders": [1] * 15 + [0] * 5, "level": 1.0, "flow": 0.0,
            "adjusted_close": np.linspace(10, 12, len(dates)), "entries": 0, "exits": 0, "age_days": 1.0, "profit_pct": 0.0,
            "reversal_21d": 0.0, "momentum_12_1": 0.0,
        }
    )
    frame = er.add_features(panel)
    obs = er.observations(frame)
    assert obs["date"].tolist() == [pd.Timestamp("2026-06-26"), pd.Timestamp("2026-07-03")]  # the 10th is past the end, the 17th has no holder
    assert obs["ret_5"].iloc[0] == pytest.approx(frame.set_index("date").loc["2026-07-03", "adjusted_close"] / frame.set_index("date").loc["2026-06-26", "adjusted_close"] - 1)
    assert obs["ret_adj_5"].iloc[0] == pytest.approx(0.0)  # one name: its own median


def test_add_features_flow_and_z_level() -> None:
    dates = pd.bdate_range("2025-01-01", periods=200)
    level = np.r_[np.full(150, 2.0), np.linspace(2.0, 4.0, 50)]
    flow = np.r_[np.zeros(150), np.full(50, 2.0 / 49)]
    panel = pd.DataFrame({"isin": "GB1", "date": dates, "n_holders": 2, "level": level, "flow": flow, "adjusted_close": 10.0,
                          "entries": 0, "exits": 0, "age_days": 1.0, "profit_pct": 0.0, "reversal_21d": 0.0, "momentum_12_1": 0.0})
    frame = er.add_features(panel)
    assert frame["flow_21"].iloc[:20].isna().all() and frame["flow_21"].iloc[100] == pytest.approx(0.0)
    assert frame["flow_21"].iloc[-1] == pytest.approx(21 * (2.0 / 49) / level[-22])
    assert frame["z_level"].iloc[:62].isna().all() and frame["z_level"].iloc[-1] > 1  # a rising level is abnormal
    assert frame["momentum_6_1"].iloc[:126].isna().all() and frame["momentum_6_1"].iloc[-1] == pytest.approx(0.0)
    assert np.isnan(frame["ret_21"].iloc[-1]) and frame["ret_21"].iloc[0] == pytest.approx(0.0)


def test_run_family_finds_a_planted_bearish_signal() -> None:
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2014-01-03", periods=60 * 5, freq="B")
    fridays = er.week_ends(dates)
    rows = []
    for d in fridays:
        n = 120
        level = rng.uniform(0.5, 5.0, size=n)
        rows.append(pd.DataFrame({
            "date": d, "isin": [f"GB{k}" for k in range(n)], "level": level, "n_holders": rng.integers(1, 6, size=n),
            "flow_21": rng.normal(size=n), "z_level": rng.normal(size=n), "age_days": rng.uniform(1, 300, size=n),
            "profit_pct": rng.normal(size=n), "reversal_21d": rng.normal(size=n), "momentum_12_1": rng.normal(size=n),
            # heavier disclosed short -> lower return (SPEC's sign); the rest is noise
            "ret_adj_5": -0.01 * level + rng.normal(scale=0.03, size=n),
            "ret_adj_10": -0.01 * level + rng.normal(scale=0.03, size=n),
            "ret_adj_21": -0.01 * level + rng.normal(scale=0.03, size=n),
        }))
    frame = pd.concat(rows, ignore_index=True)
    results = er.run_family(frame)
    assert len(results) == 54 and {r.family_size for r in results} == {54}
    table = ev.results_table(results)
    hit = table[(table.feature == "level") & (table.cleaning == "factors") & (table.horizon == 21)].iloc[0]
    assert hit.mean_ic < 0 and hit.sign_as_spec and hit.significant
    noise = table[(table.feature == "age_days") & (table.cleaning == "none")]
    assert not noise.significant.any()
    assert all(r.n_dates >= 60 for r in results)  # 300 business days from a Friday span 61 ISO weeks
