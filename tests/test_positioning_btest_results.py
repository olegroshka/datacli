"""positioning.btest_results: the borrow overlay and the summary on synthetic runs."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import btest_results as br  # noqa: E402
from positioning import export  # noqa: E402


def test_summarise_matches_hand_numbers() -> None:
    r = pd.Series([0.01, -0.01, 0.02, 0.0])
    s = br.summarise(r, periods_per_year=4)
    equity = (1 + r).cumprod()
    assert s.total_return == pytest.approx(equity.iloc[-1] - 1)
    assert s.years == 1.0 and s.cagr == pytest.approx(equity.iloc[-1] - 1)
    assert s.max_drawdown == pytest.approx(-0.01)
    assert s.sharpe == pytest.approx(r.mean() / r.std(ddof=1) * 2)
    assert np.isnan(br.summarise(pd.Series([], dtype=float)).sharpe)


def test_borrow_drag_charges_only_the_short_weights_and_only_the_excess_over_flat() -> None:
    dates = pd.date_range("2024-01-01", periods=2)
    weights = pd.DataFrame({"HARD": [-0.10, -0.10], "EASY": [-0.10, 0.0], "LONG": [0.20, 0.20], "UNKNOWN": [0.0, -0.10]}, index=dates)
    rates = pd.Series({"HARD": 0.21, "EASY": 0.003, "LONG": 0.50})
    drag = br.borrow_drag(weights, rates, flat_rate=0.01, periods_per_year=1)
    # day 1: HARD pays 20% extra on 10%, EASY gets 0.7% back on 10%, LONG is long, UNKNOWN is flat
    assert drag.iloc[0] == pytest.approx(0.10 * 0.20 - 0.10 * 0.007)
    assert drag.iloc[1] == pytest.approx(0.10 * 0.20)
    returns = pd.Series([0.05, 0.05], index=dates)
    adjusted = br.overlay(returns, weights, rates, flat_rate=0.01, periods_per_year=1)
    assert adjusted.iloc[1] == pytest.approx(0.05 - 0.02)
    profile = br.short_rate_profile(weights, rates)
    assert profile.iloc[0] == pytest.approx((0.1 * 0.21 + 0.1 * 0.003) / 0.2)
    assert profile.iloc[1] == pytest.approx(0.21)  # UNKNOWN has no rate and is left out


def test_overlay_is_a_no_op_when_every_rate_equals_the_flat_one() -> None:
    dates = pd.date_range("2024-01-01", periods=3)
    weights = pd.DataFrame({"A": [-0.5, -0.5, -0.5], "B": [0.5, 0.5, 0.5]}, index=dates)
    returns = pd.Series([0.01, 0.02, -0.01], index=dates)
    out = br.overlay(returns, weights, pd.Series({"A": 0.01}), flat_rate=0.01)
    pd.testing.assert_series_equal(out, returns)


def test_negated_name_and_write_pair(tmp_path: Path) -> None:
    assert export.negated_name("short_z_inventory_size.parquet") == "short_z_inventory_size_neg.parquet"
    wide = pd.DataFrame({"A": [0.25, -0.5]}, index=pd.date_range("2024-01-01", periods=2))
    export.write_pair(wide, tmp_path, "x.parquet")
    pd.testing.assert_frame_equal(pd.read_parquet(tmp_path / "x_neg.parquet"), -wide, check_freq=False)


def test_demeaned_rank_is_in_the_half_unit_interval_per_date() -> None:
    frame = pd.DataFrame({"date": ["d1"] * 3 + ["d2"] * 2 + ["d3"], "v": [3.0, 1.0, 2.0, 5.0, 4.0, 9.0]})
    r = export.demeaned_rank(frame, "v")
    assert r.tolist()[:3] == [0.5, -0.5, 0.0] and r.tolist()[3:5] == [0.5, -0.5] and np.isnan(r.iloc[5])
