from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from positioning import nowcast_daily as nd  # noqa: E402
from positioning import nowcast_short_interest as ns  # noqa: E402


def _daily() -> tuple[pd.DataFrame, pd.DataFrame]:
    days = pd.bdate_range("2024-04-01", "2024-06-05")
    w = ns.period_windows(["2024-04-30", "2024-05-15", "2024-05-31"], days)
    rows = [{"date": d, "symbol": "X", "short_volume": 100.0, "short_exempt_volume": 10.0, "total_volume": 200.0,
             "cvolume": 1000.0, "adjusted_close": 10.0 + 0.1 * i, "adv": 1000.0} for i, d in enumerate(days)]
    return pd.DataFrame(rows), w


def test_partial_features_accumulate_within_the_window() -> None:
    daily, w = _daily()
    out = nd.partial_flow_features(daily, w)
    first = out[out["settlement_date"] == pd.Timestamp("2024-05-15")].reset_index(drop=True)
    assert first["days_sofar"].tolist() == list(map(float, range(1, len(first) + 1)))
    assert first["sv_adv"].tolist() == pytest.approx([0.1 * k for k in range(1, len(first) + 1)])
    assert first["sr_mean"].iloc[-1] == pytest.approx(0.5) and first["exempt_share"].iloc[-1] == pytest.approx(0.1)
    assert first["abn_cvol"].iloc[-1] == pytest.approx(1.0) and first["sv_last_adv"].iloc[-1] == pytest.approx(0.5)
    assert first["ret_period"].iloc[0] == pytest.approx(0.0)
    assert set(nd.PARTIAL_FEATURES) - set(ns.STATE) <= set(out.columns)


class _Const:
    def __init__(self, value: float) -> None:
        self.value = value

    def predict(self, x: pd.DataFrame) -> np.ndarray:
        return np.full(len(x), self.value)


def test_daily_nowcast_adds_the_unpublished_period_and_the_partial_one_to_the_known_print() -> None:
    daily, w = _daily()
    partial = nd.partial_flow_features(daily, w)
    full = pd.DataFrame({
        "symbol": ["X", "X"], "settlement_date": pd.to_datetime(["2024-05-15", "2024-05-31"]), "adv": [1000.0, 1000.0], "y": [0.5, -0.3],
        **{c: [0.0, 0.0] for c in ns.STATE}, **{c: [0.0, 0.0] for c in ns.FLOW},
    })
    rows = nd.partial_rows(partial, full)
    prints = pd.DataFrame({
        "symbol": ["X", "X", "X"], "settlement_date": pd.to_datetime(["2024-04-30", "2024-05-15", "2024-05-31"]),
        "published_at": pd.to_datetime(["2024-05-09", "2024-05-24", "2024-06-11"]), "short_position": [1000.0, 1500.0, 1200.0],
    })
    out = nd.daily_nowcast(rows, full, prints, full_model=_Const(0.4), partial_model=_Const(0.1)).set_index("date")
    # 2024-05-13 (window of the 15 May print, the 30 April print known): si_pub 1000, no complete unpublished period, partial +0.1 x 1000
    assert out.loc["2024-05-13", "si_pub"] == 1000.0 and out.loc["2024-05-13", "si_hat"] == pytest.approx(1100.0)
    # 2024-05-20 (window of the 31 May print, the 15 May print not yet published): si_pub 1000, the complete 15 May period adds 0.4 x 1000
    assert out.loc["2024-05-20", "si_pub"] == 1000.0 and out.loc["2024-05-20", "si_hat"] == pytest.approx(1000.0 + 400.0 + 100.0)
    # 2024-05-28 (the 15 May print published on the 24th): si_pub 1500, nothing between, the partial only
    assert out.loc["2024-05-28", "si_pub"] == 1500.0 and out.loc["2024-05-28", "si_hat"] == pytest.approx(1600.0)
    assert (out.index > pd.Timestamp("2024-05-08")).all()  # before the first publication nothing is known


def test_path_features_are_causal_and_the_flow_is_ten_day_growth() -> None:
    dates = pd.bdate_range("2024-01-01", periods=120)
    level = np.exp(np.linspace(0.0, 1.0, 120)) * 1000.0
    frame = pd.DataFrame({"symbol": "X", "date": dates, "si_hat": level, "si_pub": level * 0.5})
    out = nd.path_features(frame)
    assert out["z_inventory_nowcast"].iloc[:nd.MIN_HISTORY_DAYS].isna().all() and np.isfinite(out["z_inventory_nowcast"].iloc[-1])
    assert out["flow_nowcast"].iloc[10] == pytest.approx(level[10] / level[0] - 1.0) and np.isnan(out["flow_nowcast"].iloc[9])
    assert out["flow_published"].iloc[10] == pytest.approx(out["flow_nowcast"].iloc[10])  # a scale does not change growth


def test_family_reads_a_planted_bearish_feature_and_its_gain() -> None:
    rng = np.random.default_rng(2)
    dates = pd.bdate_range("2022-01-03", periods=60)
    rows = []
    for d in dates:
        x = rng.normal(size=300)
        noise = rng.normal(size=300)
        for i in range(300):
            rows.append({"date": d, "symbol": f"S{i}", "z_inventory_nowcast": x[i], "z_inventory_published": rng.normal(),
                         "flow_nowcast": rng.normal(), "flow_published": rng.normal(),
                         "reversal_21d": rng.normal(), "momentum_12_1": rng.normal(), "level": rng.normal(),
                         "ret_adj_10": -0.5 * x[i] + noise[i], "ret_adj_21": -0.5 * x[i] + noise[i]})
    table = nd.family(pd.DataFrame(rows))
    assert len(table) == 2 * nd.FAMILY_SIZE
    hit = table[(table["timing"] == "nowcast") & (table["feature"] == "z_inventory") & (table["cleaning"] == "none") & (table["horizon"] == 10)].iloc[0]
    assert hit["mean_ic"] < -0.3 and hit["significant"] and hit["sign_as_spec"]
    g = nd.gains(table).set_index(["feature", "cleaning", "horizon"])["gain"]
    assert g[("z_inventory", "none", 10)] < -0.3 and abs(g[("flow", "none", 10)]) < 0.1
