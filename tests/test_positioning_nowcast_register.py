from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from positioning import nowcast_register as nr  # noqa: E402


def _prices(days: int = 80) -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=days)
    rows = []
    for ticker, drift in (("A", 0.001), ("B", -0.001)):
        close = 100.0
        for i, day in enumerate(dates):
            close *= 1.0 + drift
            volume = 1000.0 if i != days - 1 else 5000.0  # a volume spike on the last day
            rows.append({"date": day, "ticker": ticker, "high": close * 1.02, "low": close * 0.98, "close": close, "adjusted_close": close, "volume": volume})
    return pd.DataFrame(rows)


def test_ripple_features_are_per_ticker_and_the_spike_reads_as_abnormal_volume() -> None:
    out = nr.ripple_features(_prices())
    assert list(out.columns) == ["date", "ticker", *nr.RIPPLES]
    a = out[out["ticker"] == "A"].reset_index(drop=True)
    assert np.isnan(a.loc[0, "ret_1"]) and a.loc[1, "ret_1"] == pytest.approx(0.001)
    assert a.loc[5, "ret_5"] == pytest.approx(1.001**5 - 1.0) and np.isnan(a.loc[4, "ret_5"])
    assert np.isnan(a.loc[nr.ADV_MIN_PERIODS - 2, "abn_vol"]) and a.loc[nr.ADV_MIN_PERIODS, "abn_vol"] == pytest.approx(1.0)
    assert a["abn_vol"].iloc[-1] == pytest.approx(5.0) and a["pressure"].iloc[-1] == pytest.approx(5.0)
    b = out[out["ticker"] == "B"].reset_index(drop=True)
    assert b["pressure"].iloc[-1] == pytest.approx(-5.0)  # falling name, same spike
    assert a["range"].iloc[-1] == pytest.approx(0.04)
    assert out["mkt_ret"].iloc[-1] == pytest.approx(0.0, abs=1e-12)  # the median of +0.1 and -0.1 percent
    assert a["rvol_21"].iloc[-1] == pytest.approx(0.0, abs=1e-12)  # a constant drift has no volatility


def _issuers() -> pd.DataFrame:
    dates = pd.bdate_range("2024-01-01", periods=6)
    return pd.DataFrame({
        "market": "uk", "isin": "GB1", "ticker": "A", "date": dates,
        "n_holders": [0, 1, 1, 1, 0, 0], "level": [0.0, 0.6, 0.6, 0.9, 0.0, 0.0],
        "flow": [0.0, 0.6, 0.0, 0.3, -0.9, 0.0], "entries": [0, 1, 0, 0, 0, 0], "exits": [0, 0, 0, 0, 1, 0],
        "age_days": [np.nan, 0.0, 1.0, 2.0, np.nan, np.nan], "profit_pct": [np.nan, 0.0, 0.01, 0.02, np.nan, np.nan],
    })


def test_state_features_count_the_days_since_the_last_published_change() -> None:
    out = nr.state_features(_issuers())
    assert out["flow_21"].tolist() == pytest.approx([0.0, 0.6, 0.6, 0.9, 0.0, 0.0])
    assert out["days_since_event"].tolist()[1:] == pytest.approx([0.0, 1.0, 0.0, 0.0, 3.0])  # a weekend between the 5th and 6th day
    assert np.isnan(out["days_since_event"].iloc[0]) and out["market_code"].iloc[0] == nr.MARKET_CODE["uk"]


def _funds() -> pd.DataFrame:
    d = pd.to_datetime
    return pd.DataFrame({
        "isin": ["GB1", "GB1", "GB1", "GB1", "GB1"],
        "holder": ["F1", "F1", "F2", "F1", "F2"],
        "position_date": [d("2024-01-02"), d("2024-01-04"), d("2024-01-04"), d("2024-01-05"), d("2024-01-05")],
        "reset": [True, False, True, False, False],
        "flow": [0.0, 0.3, 0.0, -0.9, 0.2],
        "inventory": [0.6, 0.9, 0.5, 0.0, 0.7],
        "below_threshold": [False, False, False, True, False],
    })


def test_targets_read_entries_increases_decreases_and_exits_per_issuer_day() -> None:
    out = nr.targets(_funds()).set_index("date")
    assert out.loc["2024-01-02", ["t1", "t2"]].tolist() == [1, 0] and out.loc["2024-01-02", "t3"] == pytest.approx(0.6)  # a seed lot is an entry
    assert out.loc["2024-01-04", ["t1", "t2"]].tolist() == [1, 0] and out.loc["2024-01-04", "t3"] == pytest.approx(0.8)  # increase plus entry
    assert out.loc["2024-01-05", ["t1", "t2"]].tolist() == [1, 1] and out.loc["2024-01-05", "t3"] == pytest.approx(-0.7)  # an exit and an increase


def test_assemble_aligns_state_ripples_and_targets_and_signs_the_week_ahead() -> None:
    issuers = _issuers()
    prices = _prices(days=6).query("ticker == 'A'").copy()
    prices["date"] = issuers["date"].to_numpy()
    frame = nr.assemble(issuers, _funds(), prices)
    # the panel's days are Jan 1, 2, 3, 4, 5 and 8; the funds' rows are dated Jan 2, 4 and 5
    assert len(frame) == 6 and frame["t1"].tolist() == [0, 1, 0, 1, 1, 0] and frame["t2"].tolist() == [0, 0, 0, 0, 1, 0]
    assert frame["t3"].tolist() == pytest.approx([0.0, 0.6, 0.0, 0.8, -0.7, 0.0])
    # t4 at the first day: the next five days sum to +0.7 (up); after that there are fewer than five days ahead
    assert frame["t4"].iloc[0] == 1.0 and frame["t4"].iloc[1:].isna().all()
    assert set(nr.STATE + nr.RIPPLES) <= set(frame.columns)


def test_ahead_sign_uses_the_flat_band_and_the_next_days_only() -> None:
    frame = pd.DataFrame({"isin": ["X"] * 8, "date": pd.bdate_range("2024-01-01", periods=8), "t3": [5.0, 0.0, 0.0, 0.0, 0.0, 0.0, -0.5, 0.0]})
    sign = nr.ahead_sign(frame, days=5, band=0.01)
    assert sign.tolist()[:3] == [0.0, 2.0, 2.0] and np.isnan(sign.iloc[3])  # the own day's +5 is not ahead; day 2 sees the -0.5 on day 7


def test_split_masks_partition_the_dates() -> None:
    frame = pd.DataFrame({"date": pd.to_datetime(["2019-12-31", "2020-01-02", "2022-12-30", "2023-01-03"])})
    masks = nr.split(frame)
    assert masks["fit"].tolist() == [True, False, False, False]
    assert masks["validate"].tolist() == [False, True, True, False]
    assert masks["test"].tolist() == [False, False, False, True]


def _planted(n: int = 6000, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.to_datetime(pd.Series(rng.integers(0, 4000, n), name="d").map(lambda k: pd.Timestamp("2013-01-01") + pd.Timedelta(days=int(k))))
    frame = pd.DataFrame({"date": dates.to_numpy(), "isin": [f"I{i % 50}" for i in range(n)]})
    for column in nr.STATE:
        frame[column] = rng.normal(size=n)
    frame["market_code"] = rng.integers(0, 3, n).astype(float)
    for column in nr.RIPPLES:
        frame[column] = rng.normal(size=n)
    logit = 2.5 * frame["abn_vol"] + 0.3 * frame["level"] - 3.0  # the ripple carries the target, the state only a little
    p = 1.0 / (1.0 + np.exp(-logit))
    frame["t1"] = (rng.uniform(size=n) < p).astype(int)
    frame["t2"] = (rng.uniform(size=n) < 0.05).astype(int)  # nothing carries t2
    frame["t3"] = 1.5 * frame["pressure"] + rng.normal(scale=0.5, size=n)
    frame["t4"] = rng.integers(0, 3, n).astype(float)
    return frame


def test_run_reports_a_positive_ripple_increment_where_the_ripples_carry_the_target() -> None:
    frame = _planted()
    table, increments = nr.run(frame, targets_to_run=("t1", "t2", "t3"), models=("linear",))
    assert set(table["features"]) == {"state", "state+ripples"} and set(table["years"]) == {"validate", "test"}
    inc = increments.set_index(["target", "model", "years", "metric"])["increment"]
    assert inc[("t1", "linear", "test", "auc")] > 0.15 and inc[("t1", "linear", "validate", "auc")] > 0.15
    assert abs(inc[("t2", "linear", "test", "auc")]) < 0.08  # nothing to find
    assert inc[("t3", "linear", "test", "r2")] > 0.5
    boosted = nr.fit_score(frame, "t4", features=nr.STATE + nr.RIPPLES, features_label="state+ripples", model="boost", masks=nr.split(frame), iterations=(20,))
    assert {s.metric for s in boosted} == {"macro_auc"} and all(0.3 < s.value < 0.7 for s in boosted)
    assert "| target |" in nr.markdown(increments.head(2))
