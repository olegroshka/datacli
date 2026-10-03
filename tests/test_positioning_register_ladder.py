"""positioning.register_ladder: the per-fund ladder on register rows and the issuer panel, on synthetic data."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from positioning import register_ladder as rl  # noqa: E402

DATES = pd.bdate_range("2024-01-01", "2024-03-29")
CLOSES = pd.Series(np.linspace(100.0, 80.0, len(DATES)), index=DATES)  # the stock falls: shorts profit


def _register(rows: list[tuple]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, columns=["holder", "isin", "net_short_pct", "position_date", "published_from"])
    frame["market"] = "uk"
    frame["holder_lei"] = None
    frame["issuer"] = "X PLC"
    frame["published_to"] = None
    frame["file_date"] = pd.Timestamp("2026-07-10")
    return frame


def test_prepare_rows_dedupes_orders_and_dates_visibility_monotonically() -> None:
    reg = _register(
        [
            ("A", "GB1", 0.6, "2024-01-10", "2024-01-11"),
            ("A", "GB1", 0.6, "2024-01-10", "2024-01-11"),  # exact duplicate
            ("A", "GB1", 0.9, "2024-01-10", "2024-01-12"),  # same day, last stands
            ("A", "GB1", 1.2, "2024-01-20", "2024-01-22"),
            ("A", "GB1", 1.0, "2024-01-15", "2024-02-05"),  # a late correction, published after the 20th's row
            ("A", "GB1", 0.4, "2024-02-10", "2024-02-12"),  # below the threshold: closes visibility
        ]
    )
    rows = rl.prepare_rows(reg)
    assert len(rows) == 4 and rows["net_short_pct"].tolist() == [0.9, 1.0, 1.2, 0.4]
    assert rows["visible_from"].dt.strftime("%Y-%m-%d").tolist() == ["2024-01-12", "2024-02-05", "2024-02-05", "2024-02-12"]
    assert rows["late"].tolist() == [False, True, False, False] and rows["below_threshold"].tolist() == [False, False, False, True]


def test_fund_ladder_prices_lots_at_the_position_date_and_signs_short_profit() -> None:
    reg = _register(
        [
            ("A", "GB1", 0.6, "2024-01-10", "2024-01-11"),
            ("A", "GB1", 1.2, "2024-01-20", "2024-01-22"),  # a Saturday: priced at Friday's close
            ("A", "GB1", 0.8, "2024-02-05", "2024-02-06"),
            ("A", "GB1", 0.3, "2024-02-20", "2024-02-21"),
        ]
    )
    rows = rl.prepare_rows(reg)
    out = pd.DataFrame(rl.fund_ladder(rows, CLOSES))
    assert out["reset"].tolist() == [True, False, False, False]
    assert out["flow"].tolist() == pytest.approx([0.0, 0.6, -0.4, -0.5])
    assert out["inventory"].tolist() == pytest.approx([0.6, 1.2, 0.8, 0.3])
    assert out["lot_price"].iloc[1] == pytest.approx(float(CLOSES.asof(pd.Timestamp("2024-01-19"))))
    assert out["seed_share"].iloc[0] == 1.0 and out["seed_share"].iloc[1] == pytest.approx(0.5)
    assert out["profit_pct"].iloc[1] > 0  # the price fell after the lots were opened: a short in profit
    assert out["wavg_age_days"].iloc[1] == pytest.approx(0.5 * 10 + 0.5 * 0)
    assert out["n_lots"].iloc[2] == 2  # FIFO: 0.4 came off the seed lot, 0.2 of it is left beside the 0.6 lot
    assert out["below_threshold"].tolist() == [False, False, False, True]
    assert rl.fund_ladder(rows, pd.Series(dtype=float, index=pd.DatetimeIndex([]))) == []
    assert rl.fund_ladder(rows, CLOSES[CLOSES.index >= "2024-02-01"])[0]["position_date"] == pd.Timestamp("2024-02-05")


def test_issuer_panel_counts_visible_holders_point_in_time_and_marks_age_and_profit() -> None:
    reg = _register(
        [
            ("A", "GB1", 0.6, "2024-01-10", "2024-01-11"),
            ("B", "GB1", 1.0, "2024-01-15", "2024-01-16"),
            ("A", "GB1", 1.2, "2024-01-20", "2024-01-22"),
            ("B", "GB1", 0.4, "2024-02-05", "2024-02-06"),  # B drops out
            ("C", "GB2", 0.7, "2024-01-30", "2024-01-31"),
        ]
    )
    rows = rl.prepare_rows(reg)
    funds = rl.fund_ladders(rows, {"GB1": CLOSES, "GB2": CLOSES})
    assert set(funds["holder"]) == {"A", "B", "C"}
    panel = rl.issuer_panel(funds, DATES, {"GB1": CLOSES, "GB2": CLOSES})
    gb1 = panel[panel["isin"] == "GB1"].set_index("date")
    assert gb1.index[0] == pd.Timestamp("2024-01-11")
    assert gb1.loc["2024-01-11", "n_holders"] == 1 and gb1.loc["2024-01-11", "level"] == pytest.approx(0.6)
    assert gb1.loc["2024-01-15", "n_holders"] == 1  # B's row is not visible until the 16th
    assert gb1.loc["2024-01-16", "n_holders"] == 2 and gb1.loc["2024-01-16", "level"] == pytest.approx(1.6)
    assert gb1.loc["2024-01-16", "entries"] == 1 and gb1.loc["2024-01-16", "flow"] == pytest.approx(1.0)
    assert gb1.loc["2024-01-22", "level"] == pytest.approx(2.2) and gb1.loc["2024-01-22", "flow"] == pytest.approx(0.6)
    assert gb1.loc["2024-02-06", "n_holders"] == 1 and gb1.loc["2024-02-06", "exits"] == 1
    assert gb1.loc["2024-02-06", "level"] == pytest.approx(1.2) and gb1.loc["2024-02-06", "flow"] == pytest.approx(-0.6)
    # age on 2024-01-18: A's seed lot opened on the 10th, 8 days old; B's on the 15th, 3 days: weighted by percent
    assert gb1.loc["2024-01-18", "age_days"] == pytest.approx((0.6 * 8 + 1.0 * 3) / 1.6)
    # profit: shorts gain as the close falls below the percent-weighted cost
    cost_a = float(CLOSES.asof(pd.Timestamp("2024-01-10")))
    cost_b = float(CLOSES.asof(pd.Timestamp("2024-01-15")))
    inv_cost = (0.6 / cost_a + 1.0 / cost_b) / 1.6
    assert gb1.loc["2024-01-18", "profit_pct"] == pytest.approx(1 - float(CLOSES.loc["2024-01-18"]) * inv_cost)
    assert (gb1["profit_pct"].dropna() > 0).all()
    assert gb1["close"].notna().all()
    gb2 = panel[panel["isin"] == "GB2"].set_index("date")
    assert gb2.index[0] == pd.Timestamp("2024-01-31") and gb2["n_holders"].iloc[-1] == 1
    assert list(panel.columns) == list(rl.PANEL_COLUMNS)
    assert rl.issuer_panel(funds.iloc[0:0], DATES, {}).empty
