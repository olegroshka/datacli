"""finra.panel + scoring.positioning_eval: point-in-time features and the per-day tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import panel as fpanel  # noqa: E402
from scoring import positioning_eval as pos  # noqa: E402


def _view(con, rows: list[tuple[str, str, float]]) -> None:
    frame = pd.DataFrame(rows, columns=["eodhd_code", "date", "short_ratio"])
    frame["date"] = pd.to_datetime(frame["date"]).dt.date
    frame["security_kind"] = "common"
    con.register("_sv", frame)
    con.execute("CREATE OR REPLACE VIEW finra_short_volume AS SELECT * FROM _sv")


def test_known_features_use_only_prior_rows() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    days = pd.bdate_range("2026-09-01", periods=8)
    ratios = [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80]
    _view(con, [("AAA", d.strftime("%Y-%m-%d"), r) for d, r in zip(days, ratios)])
    f = fpanel.short_ratio_features(
        con, start=days[0], end=days[-1], windows=(1, 3), baseline=4, short_window=3
    )
    f = f.set_index("date")
    # day 5 (0.50): the 3-row window ending at T is (0.30, 0.40, 0.50); the known one ends at T-1
    d5 = f.loc[days[4]]
    assert d5["sr3_t"] == pytest.approx(0.40)
    assert d5["sr3_known"] == pytest.approx(0.30)  # (0.20, 0.30, 0.40)
    assert d5["sr1_known"] == pytest.approx(0.40)  # yesterday's ratio
    assert d5["n_rows_known"] == 4
    # abnormal = 3-row mean minus 4-row mean, shifted the same way
    assert d5["sr_abn_t"] == pytest.approx(0.40 - np.mean([0.20, 0.30, 0.40, 0.50]))
    assert d5["sr_abn_known"] == pytest.approx(0.30 - np.mean([0.10, 0.20, 0.30, 0.40]))
    # the very first row knows nothing
    first = f.loc[days[0]]
    assert pd.isna(first["sr1_known"]) and first["n_rows_known"] == 0


def test_features_are_per_symbol_and_clipped() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    days = pd.bdate_range("2026-09-01", periods=4)
    rows = [("AAA", d.strftime("%Y-%m-%d"), 0.5) for d in days] + [
        ("BBB", d.strftime("%Y-%m-%d"), 0.1) for d in days
    ]
    _view(con, rows)
    f = fpanel.short_ratio_features(
        con, start=days[2], end=days[3], windows=(1, 2), baseline=3, short_window=2
    )
    assert sorted(f["eodhd_code"].unique()) == ["AAA", "BBB"]
    assert f["date"].min() == days[2]  # clipped, but history before the clip was used
    assert f[f.eodhd_code == "BBB"]["sr2_known"].round(6).tolist() == [0.1, 0.1]
    with pytest.raises(ValueError, match="short_window"):
        fpanel.short_ratio_features(
            con, start=days[0], end=days[3], windows=(1,), short_window=5
        )


def test_attach_positioning_joins_on_ticker_and_trading_day() -> None:
    panel = pd.DataFrame(
        {
            "symbol": ["AAPL.US", "AAPL.US", "ZZZ.US"],
            "date": pd.to_datetime(
                ["2026-09-05", "2026-09-07", "2026-09-07"]
            ),  # Sat, Mon, Mon
            "trade_date": pd.to_datetime(["2026-09-08", "2026-09-08", "2026-09-08"]),
            "mat_max": [2, 1, 3],
            "f1_ex": [0.02, -0.01, 0.05],
        }
    )
    features = pd.DataFrame(
        {
            "eodhd_code": ["AAPL", "ZZZ"],
            "date": pd.to_datetime(["2026-09-08", "2026-09-08"]),
            "sr5_known": [0.5, 0.4],
            "n_rows_known": [60, 3],
        }
    )
    merged, rep = fpanel.attach_positioning(panel, features, min_history=20)
    assert len(merged) == 2 and set(merged["symbol"]) == {"AAPL.US"}
    assert rep == {
        "panel_rows": 3,
        "matched_rows": 3,
        "dropped_thin_history": 1,
        "rows": 2,
        "match_share": 1.0,
        "n_days": 1,
    }


def _synthetic(n_days: int = 40, n: int = 120, seed: int = 0) -> pd.DataFrame:
    """|move| grows with materiality, and more so when the short ratio is high."""
    rng = np.random.default_rng(seed)
    rows = []
    for d in pd.bdate_range("2026-06-01", periods=n_days):
        mat = rng.integers(0, 4, n)
        sr = rng.uniform(0.2, 0.8, n)
        scale = 0.01 + 0.004 * mat + 0.01 * mat * (sr - 0.5)
        ret = rng.normal(0, 1, n) * scale
        rows.append(
            pd.DataFrame(
                {"trade_date": d, "mat_max": mat, "sr5_known": sr, "f1_ex": ret}
            )
        )
    return pd.concat(rows, ignore_index=True)


def test_interaction_tests_detect_a_planted_effect() -> None:
    d = _synthetic()
    table, st = pos.interaction_by_tercile(d, pos_field="sr5_known")
    assert list(table["tercile"]) == ["low", "mid", "high"]
    assert st["diff_t"] > 3 and st["diff_mean"] > 0
    table, st = pos.conditional_magnitude(d, pos_field="sr5_known")
    assert st["spread_t"] > 3 and st["spread_mean_bps"] > 0
    assert list(table.columns) == [
        "mat_max",
        "low",
        "mid",
        "high",
        "low_n",
        "mid_n",
        "high_n",
    ]
    assert (table[["low_n", "mid_n", "high_n"]].sum(axis=1) > 0).all()
    table, st = pos.fama_macbeth_ranks(d, pos_field="sr5_known")
    by = table.set_index("term")
    assert by.loc["mat", "t"] > 3 and by.loc["mat_x_pos", "t"] > 3
    assert st["nw_lags"] == 0
    alone = pos.positioning_alone(d, field="sr5_known")
    assert alone["corr_n_days"] == 40


def test_interaction_tests_are_flat_without_an_effect() -> None:
    rng = np.random.default_rng(1)
    rows = []
    for d in pd.bdate_range("2026-06-01", periods=40):
        rows.append(
            pd.DataFrame(
                {
                    "trade_date": d,
                    "mat_max": rng.integers(0, 4, 100),
                    "sr5_known": rng.uniform(0, 1, 100),
                    "f1_ex": rng.normal(0, 0.02, 100),
                }
            )
        )
    d = pd.concat(rows, ignore_index=True)
    _, st = pos.interaction_by_tercile(d, pos_field="sr5_known")
    assert abs(st["diff_t"]) < 2.5
    table, _ = pos.fama_macbeth_ranks(d, pos_field="sr5_known", horizon="f1_ex")
    assert (table["t"].abs() < 2.5).all()


def test_bonferroni_note() -> None:
    assert "p < 0.0100" in pos.bonferroni_note(5)
