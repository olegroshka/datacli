"""The next-day forecast's features must not see the future, and its walk-forward must not train on the block."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

_PATH = Path(__file__).resolve().parents[1] / "scripts" / "chip_nextday_forecast.py"
_spec = importlib.util.spec_from_file_location("chip_nextday_forecast", _PATH)
fc = importlib.util.module_from_spec(_spec)
sys.modules["chip_nextday_forecast"] = fc
_spec.loader.exec_module(fc)


def _bars(n=400, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    index = pd.bdate_range("2015-01-01", periods=n)
    close = 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n)))
    return pd.DataFrame(
        {
            "open": close * (1 + rng.normal(0, 0.005, n)),
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "a": close,
            "volume": rng.integers(1000, 5000, n).astype(float),
        },
        index=index,
    )


def _same(left: pd.DataFrame, right: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(left.reindex(right.index), right, check_exact=False, rtol=1e-12, atol=1e-12)


def test_stock_features_do_not_change_when_the_future_is_removed():
    bars = _bars()
    _same(fc.stock_features(bars), fc.stock_features(bars.iloc[:300]))


def test_changing_future_bars_leaves_the_past_alone():
    bars = _bars()
    altered = bars.copy()
    altered.iloc[300:, altered.columns.get_loc("a")] *= 3.0
    altered.iloc[300:, altered.columns.get_loc("close")] *= 3.0
    pd.testing.assert_frame_equal(fc.stock_features(bars).iloc[:300], fc.stock_features(altered).iloc[:300])


def test_no_infinities_from_a_zero_range_or_zero_volume():
    bars = _bars()
    bars.iloc[100, bars.columns.get_loc("high")] = bars.iloc[100]["close"]
    bars.iloc[100, bars.columns.get_loc("low")] = bars.iloc[100]["close"]
    bars.iloc[120:130, bars.columns.get_loc("volume")] = 0.0
    assert not np.isinf(fc.stock_features(bars).to_numpy(dtype=float)).any()


def _funds(n=300) -> pd.DataFrame:
    rng = np.random.default_rng(2)
    index = pd.bdate_range("2015-01-01", periods=n)
    return pd.DataFrame({name: 100 * np.exp(np.cumsum(rng.normal(0, 0.01, n))) for name in fc.FUNDS}, index=index)


def _fred(index: pd.DatetimeIndex) -> pd.DataFrame:
    return pd.DataFrame({name: np.arange(1.0, len(index) + 1.0) for name in fc.FRED}, index=index)


def test_fred_is_only_known_the_next_session():
    funds = _funds()
    macro = fc.macro_features(funds, _fred(funds.index))
    assert np.isnan(macro["vix"].iloc[0])
    assert macro["vix"].iloc[10] == 10.0  # the value stamped on row 9, whose value is 10, the row before


def test_stale_fred_carries_forward_and_never_borrows_later_values():
    funds = _funds()
    observed = _fred(funds.index).iloc[[0, 50]]
    macro = fc.macro_features(funds, observed)
    assert (macro["vix"].iloc[2:51] == observed["VIXCLS"].iloc[0]).all()


def test_news_on_a_date_is_not_visible_until_the_next_date():
    index = pd.bdate_range("2021-01-04", periods=10)
    polarity = pd.Series(0.0, index=index)
    polarity.iloc[5] = 0.9  # published on index[5], perhaps after that day's close
    x = fc.news_features(index, polarity, pd.Series(1.0, index=index))
    assert x["pol1"].iloc[5] == 0.0
    assert x["pol1"].iloc[6] == 0.9


def test_a_stretch_of_constant_article_counts_gives_no_infinity():
    index = pd.bdate_range("2021-01-04", periods=120)
    articles = pd.Series(3.0, index=index)
    articles.iloc[100] = 40.0
    x = fc.news_features(index, pd.Series(0.1, index=index), articles)
    assert not np.isinf(x.to_numpy(dtype=float)).any()


def _panel(n=900) -> pd.DataFrame:
    rng = np.random.default_rng(4)
    index = pd.bdate_range("2012-01-02", periods=n)
    return pd.DataFrame({"ticker": "AMD", "r1": rng.normal(0, 0.02, n), "y": rng.normal(0, 0.02, n)}, index=index)


def test_walk_forward_never_trains_on_the_block_or_its_eve(monkeypatch):
    panel = _panel()
    seen = []

    class Recorder:
        def fit(self, x, y):
            seen.append(x.index.max())
            return self

        def predict(self, x):
            return np.zeros(len(x))

    monkeypatch.setattr(fc, "model", lambda kind: Recorder())
    start = str(panel.index[400].date())
    out = fc.walk_forward(panel, ["r1"], "ridge", "amd", start=start)
    blocks = [out.index[i] for i in range(0, len(out), fc.REFIT)]
    assert len(seen) == len(blocks)
    for latest, block_start in zip(seen, blocks):
        assert latest < block_start - pd.Timedelta(days=1)


def test_score_charges_costs_on_changes_of_position_only():
    index = pd.bdate_range("2020-01-01", periods=4)
    y = pd.Series(0.0, index=index)
    row = fc.score(pd.Series([1.0, 1.0, -1.0, -1.0], index=index), y)
    assert row["trades_py"] == pytest.approx(2 / (4 / 252))
    assert row["stratCAGR"] < 0


def test_score_is_not_fooled_by_a_constant_long_position():
    index = pd.bdate_range("2020-01-01", periods=300)
    rng = np.random.default_rng(5)
    y = pd.Series(rng.normal(0.001, 0.02, 300), index=index)
    row = fc.score(pd.Series(1.0, index=index), y)
    assert row["pctLong"] == 1.0
    assert row["trades_py"] == pytest.approx(1 / (300 / 252))
