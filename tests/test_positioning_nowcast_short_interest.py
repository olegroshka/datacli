from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from positioning import nowcast_short_interest as ns  # noqa: E402


def test_settlement_lag_switches_to_one_day_from_may_2024() -> None:
    assert ns.settlement_lag(dt.date(2024, 5, 15)) == 2 and ns.settlement_lag(dt.date(2024, 5, 28)) == 1 and ns.settlement_lag(pd.Timestamp("2025-01-15")) == 1


def test_period_windows_shift_back_by_the_lag_and_tile_the_calendar() -> None:
    days = pd.bdate_range("2024-04-01", "2024-07-10")
    settles = ["2024-04-30", "2024-05-15", "2024-05-31", "2024-06-14"]
    w = ns.period_windows(settles, days)
    assert w["settlement_date"].dt.strftime("%Y-%m-%d").tolist() == ["2024-05-15", "2024-05-31", "2024-06-14"]
    # 2024-05-15 settles with a two-day lag: its window ends two trading days before, 2024-05-13
    assert w.loc[0, "end"] == pd.Timestamp("2024-05-13") and w.loc[0, "start_excl"] == pd.Timestamp("2024-04-26")
    # 2024-05-31 settles with a one-day lag: its window ends 2024-05-30 and starts after the previous window's end
    assert w.loc[1, "end"] == pd.Timestamp("2024-05-30") and w.loc[1, "start_excl"] == pd.Timestamp("2024-05-13")
    # a settlement on a Saturday uses the last trading day before it
    w2 = ns.period_windows(["2024-06-14", "2024-06-29"], days)
    assert w2.loc[0, "end"] == pd.Timestamp("2024-06-27")  # Friday 28th minus one trading day


def test_assign_periods_puts_each_trade_date_in_one_window_or_none() -> None:
    days = pd.bdate_range("2024-04-01", "2024-07-10")
    w = ns.period_windows(["2024-04-30", "2024-05-15", "2024-05-31"], days)
    dates = pd.Series(pd.to_datetime(["2024-04-26", "2024-04-29", "2024-05-13", "2024-05-14", "2024-05-30", "2024-05-31"]))
    got = ns.assign_periods(dates, w)
    assert got.dt.strftime("%Y-%m-%d").tolist()[:5] == ["NaT", "2024-05-15", "2024-05-15", "2024-05-31", "2024-05-31"] or pd.isna(got.iloc[0])
    assert got.iloc[1] == pd.Timestamp("2024-05-15") and got.iloc[2] == pd.Timestamp("2024-05-15")
    assert got.iloc[3] == pd.Timestamp("2024-05-31") and got.iloc[4] == pd.Timestamp("2024-05-31") and pd.isna(got.iloc[5])


def _daily() -> tuple[pd.DataFrame, pd.DataFrame]:
    days = pd.bdate_range("2024-04-01", "2024-06-05")
    w = ns.period_windows(["2024-04-30", "2024-05-15", "2024-05-31"], days)
    rows = []
    for i, day in enumerate(days):
        rows.append({"date": day, "symbol": "X", "short_volume": 100.0 + i, "short_exempt_volume": 10.0, "total_volume": 200.0 + i,
                     "cvolume": 1000.0, "adjusted_close": 10.0 + 0.1 * i, "adv": 1000.0})
    return pd.DataFrame(rows), w


def test_flow_features_sum_the_window_and_scale_by_the_median_volume() -> None:
    daily, w = _daily()
    out = ns.flow_features(daily, w).set_index("settlement_date")
    first = out.loc["2024-05-15"]
    window = daily[(daily["date"] > pd.Timestamp("2024-04-26")) & (daily["date"] <= pd.Timestamp("2024-05-13"))]
    assert first["days"] == len(window) and first["sv_adv"] == pytest.approx(window["short_volume"].sum() / 1000.0)
    assert first["sr_mean"] == pytest.approx(window["short_volume"].sum() / window["total_volume"].sum())
    assert first["exempt_share"] == pytest.approx(10.0 * len(window) / window["short_volume"].sum())
    assert first["abn_cvol"] == pytest.approx(1.0) and first["cvol_adv"] == pytest.approx(float(len(window)))
    assert first["ret_period"] == pytest.approx(window["adjusted_close"].iloc[-1] / window["adjusted_close"].iloc[0] - 1.0)
    assert first["sv_last_adv"] == pytest.approx(window["short_volume"].iloc[-5:].sum() / 1000.0)
    assert first["log_adv"] == pytest.approx(np.log(1000.0))


def test_assemble_targets_consecutive_prints_only_and_drops_thin_windows() -> None:
    daily, w = _daily()
    flows = ns.flow_features(daily, w)
    si = pd.DataFrame({
        "symbol": ["X", "X", "X", "Y", "Y"],
        "settlement_date": pd.to_datetime(["2024-04-30", "2024-05-15", "2024-05-31", "2024-04-30", "2024-05-31"]),
        "short_position": [1000.0, 1500.0, 1200.0, 500.0, 900.0], "days_to_cover": [1.0, 1.5, 1.2, 0.5, 0.9],
    })
    flows_y = flows.copy(); flows_y["symbol"] = "Y"
    frame = ns.assemble(si, pd.concat([flows, flows_y]), w)
    x = frame[frame["symbol"] == "X"].set_index("settlement_date")
    assert x.loc["2024-05-15", "y"] == pytest.approx(0.5) and x.loc["2024-05-31", "y"] == pytest.approx(-0.3)
    assert x.loc["2024-05-15", "level_adv"] == pytest.approx(1.0) and x.loc["2024-05-15", "dtc"] == pytest.approx(1.0)
    assert np.isnan(x.loc["2024-05-15", "d_prev1"]) and x.loc["2024-05-31", "d_prev1"] == pytest.approx(0.5)
    assert "Y" not in set(frame["symbol"])  # Y skipped the 15 May print: its 31 May change spans two periods


def _planted(n: int = 5000, seed: int = 1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.to_datetime(rng.choice(pd.bdate_range("2019-01-15", "2026-06-15", freq="15D"), n))
    frame = pd.DataFrame({"symbol": [f"S{i % 300}" for i in range(n)], "settlement_date": dates, "date": dates})
    for c in ns.STATE + ns.FLOW:
        frame[c] = rng.normal(size=n)
    frame["y"] = 2.0 * frame["sv_adv"] - 1.0 * frame["sv_last_adv"] + 0.2 * frame["d_prev1"] + rng.normal(scale=0.5, size=n)
    return frame


def test_run_reads_a_flow_increment_where_the_flow_carries_the_change() -> None:
    table, increments = ns.run(_planted(), models=("linear",), min_names=10)
    inc = increments.set_index(["model", "years", "metric"])["increment"]
    assert inc[("linear", "test", "r2")] > 0.5 and inc[("linear", "validate", "r2")] > 0.5
    assert inc[("linear", "test", "rank_ic")] > 0.3
    assert set(table["metric"]) == {"r2", "rank_ic"} and set(table["features"]) == {"state", "state+flow"}
    assert ns.winsorise(pd.Series([0.0, 1.0, 100.0]), limits=(0.0, 0.5)).max() == pytest.approx(1.0)
