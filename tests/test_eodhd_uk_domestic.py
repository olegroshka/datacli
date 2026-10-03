"""The uk_domestic lane: universe selection, the register flag and the fetcher wrapper's wiring."""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
for p in (_REPO_ROOT, _REPO_ROOT / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import eodhd_datasets as reg  # noqa: E402
import fetch_eodhd_uk_universe as universe  # noqa: E402


def _row(code: str, currency: str = "GBX", kind: str = "Common Stock", isin: str = "") -> dict:
    return {"Code": code, "Name": code, "Country": "UK", "Exchange": "LSE", "Currency": currency, "Type": kind, "Isin": isin}


def test_select_universe_keeps_sterling_common_stocks_and_register_names_of_any_currency() -> None:
    active = [
        _row("BA", isin="GB0002634946"),
        _row("0A00", currency="EUR", isin="NL0013267909"),  # an international listing, dropped
        _row("USDN", currency="USD", isin="GB00USD00001"),  # a dollar-quoted UK name in the register, kept
        _row("IUSA", kind="ETF"),
        _row("", isin="GB0000000000"),
    ]
    delisted = [
        _row("DEAD", isin="GB00DEAD0001"),
        _row("BA", isin="GB0002634946"),  # the active row wins
    ]
    frame, unmatched = universe.select_universe(active, delisted, {"GB0002634946", "GB00USD00001", "GB00DEAD0001", "GB00NOWHERE1"})
    assert frame["Code"].tolist() == ["BA", "USDN", "DEAD"]  # register first, then live before delisted
    assert frame["in_register"].tolist() == [True, True, True] and frame["delisted"].tolist() == [False, False, True]
    assert unmatched == {"GB00NOWHERE1"}
    plain, _ = universe.select_universe([_row("ZZZ"), _row("YYY", currency="GBP")], [], set())
    assert plain["Code"].tolist() == ["YYY", "ZZZ"] and not plain["in_register"].any()


def test_lane_is_registered_like_the_extended_lane() -> None:
    lane = reg.LANES["uk_domestic"]
    assert lane.universe_fetcher == "fetch_eodhd_uk_universe.py"
    assert lane.universe_path is not None and lane.universe_path.name == "tickers_UK.parquet"
    assert [d.kind for d in lane.datasets] == ["prices"]
    assert lane.default_exchange == "LSE" and lane.universe_code_column == "Code"


def test_wrapper_points_the_shared_fetcher_at_this_lane_and_filters_targets(tmp_path: Path, monkeypatch) -> None:
    import fetch_eodhd_uk_prices as prices
    import fetch_eodhd_us_etf_prices as etf_prices

    saved = (etf_prices.PRICES_PATH, etf_prices.PRICES_STATE_PATH, etf_prices.ETF_TICKERS_PATH, etf_prices.load_target_tickers, etf_prices.log)
    try:
        prices.configure()
        for path in (etf_prices.PRICES_PATH, etf_prices.PRICES_STATE_PATH, etf_prices.ETF_TICKERS_PATH):
            assert path.parent.name == "uk_domestic"
        assert etf_prices.ETF_TICKERS_PATH == universe.TICKERS_PATH
    finally:
        (etf_prices.PRICES_PATH, etf_prices.PRICES_STATE_PATH, etf_prices.ETF_TICKERS_PATH, etf_prices.load_target_tickers, etf_prices.log) = saved

    frame = pd.DataFrame({"Code": ["BA", "DEAD", "OTHER"], "delisted": [False, True, False], "in_register": [True, True, False]})
    frame.to_parquet(tmp_path / "u.parquet")
    monkeypatch.setattr(prices, "TICKERS_PATH", tmp_path / "u.parquet")
    monkeypatch.setattr(sys, "argv", ["x"])
    assert prices.load_target_tickers(explicit_specs=[]) == [("BA", "LSE"), ("OTHER", "LSE")]
    monkeypatch.setattr(sys, "argv", ["x", "--include-delisted", "--register-only"])
    assert prices.load_target_tickers(explicit_specs=[]) == [("BA", "LSE"), ("DEAD", "LSE")]
    assert sys.argv == ["x"]
    assert prices.load_target_tickers(explicit_specs=["BA.LSE"]) == [("BA", "LSE")]
