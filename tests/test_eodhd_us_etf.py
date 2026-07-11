from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_us_etf_prices as etf_prices  # type: ignore  # noqa: E402
import fetch_eodhd_us_etf_universe as etf_universe  # type: ignore  # noqa: E402

EXPECTED_TICKERS = [("QQQ", "US"), ("SPY", "US")]


def test_build_starter_universe_preserves_curated_membership() -> None:
    provider_df = pd.DataFrame(
        [
            {
                "Code": "SPY",
                "Name": "SPDR S&P 500 ETF Trust",
                "Type": "ETF",
                "Exchange": "NYSE ARCA",
                "Currency": "USD",
                "Isin": "US78462F1030",
            },
            {
                "Code": "QQQ",
                "Name": "Invesco QQQ Trust",
                "Type": "ETF",
                "Exchange": "NASDAQ",
                "Currency": "USD",
                "Isin": "US46090E1038",
            },
        ]
    )
    starter = etf_universe.build_starter_universe(provider_df)

    assert set(starter["ticker"]) >= {"SPY", "QQQ"}
    assert (
        starter[starter["ticker"] == "SPY"]["provider_exchange"].iloc[0] == "NYSE ARCA"
    )


def test_load_target_tickers_uses_provider_file_by_default(tmp_path: Path) -> None:
    provider_path = tmp_path / "tickers_US_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "SPY", "Name": "SPDR S&P 500 ETF Trust"},
            {"Code": "QQQ", "Name": "Invesco QQQ Trust"},
        ]
    ).to_parquet(provider_path, index=False)

    assert (
        etf_universe.load_target_tickers(
            explicit_specs=[], limit=0, provider_path=provider_path
        )
        == EXPECTED_TICKERS
    )


def test_load_target_tickers_can_use_starter_file(tmp_path: Path) -> None:
    starter_path = tmp_path / "starter_universe.csv"
    pd.DataFrame(
        [
            {"ticker": "SPY", "exchange": "US"},
            {"ticker": "QQQ", "exchange": "US"},
        ]
    ).to_csv(starter_path, index=False)

    assert (
        etf_universe.load_target_tickers(
            explicit_specs=[], limit=0, universe="starter", starter_path=starter_path
        )
        == EXPECTED_TICKERS
    )


def test_load_target_tickers_uses_monkeypatched_default_provider_path(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_US_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "SPY", "Name": "SPDR S&P 500 ETF Trust"},
            {"Code": "QQQ", "Name": "Invesco QQQ Trust"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(etf_universe, "ETF_TICKERS_PATH", provider_path)

    assert (
        etf_universe.load_target_tickers(explicit_specs=[], limit=0) == EXPECTED_TICKERS
    )


def test_load_target_tickers_uses_monkeypatched_default_starter_path(
    tmp_path: Path, monkeypatch
) -> None:
    starter_path = tmp_path / "starter_universe.csv"
    pd.DataFrame(
        [
            {"ticker": "SPY", "exchange": "US"},
            {"ticker": "QQQ", "exchange": "US"},
        ]
    ).to_csv(starter_path, index=False)
    monkeypatch.setattr(etf_universe, "STARTER_UNIVERSE_PATH", starter_path)

    assert (
        etf_universe.load_target_tickers(explicit_specs=[], limit=0, universe="starter")
        == EXPECTED_TICKERS
    )


def test_etf_price_loader_prefers_explicit_specs() -> None:
    assert etf_prices.load_target_tickers(
        explicit_specs=["SPY.US", "QQQ.US"],
        limit=0,
        provider_path=Path("unused.parquet"),
        starter_path=Path("unused.csv"),
    ) == [
        ("SPY", "US"),
        ("QQQ", "US"),
    ]


def test_etf_price_loader_defaults_to_provider_universe(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_US_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "SPY", "Name": "SPDR S&P 500 ETF Trust"},
            {"Code": "QQQ", "Name": "Invesco QQQ Trust"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(etf_prices, "ETF_TICKERS_PATH", provider_path)

    assert (
        etf_prices.load_target_tickers(explicit_specs=[], limit=0) == EXPECTED_TICKERS
    )


def test_etf_price_loader_can_use_starter_universe(tmp_path: Path, monkeypatch) -> None:
    provider_path = tmp_path / "tickers_US_ETF.parquet"
    starter_path = tmp_path / "starter_universe.csv"
    pd.DataFrame([{"Code": "DUMMY", "Name": "Unused ETF"}]).to_parquet(
        provider_path, index=False
    )
    pd.DataFrame(
        [
            {"ticker": "SPY", "exchange": "US"},
            {"ticker": "QQQ", "exchange": "US"},
        ]
    ).to_csv(starter_path, index=False)
    monkeypatch.setattr(etf_prices, "ETF_TICKERS_PATH", provider_path)
    monkeypatch.setattr(etf_prices, "STARTER_UNIVERSE_PATH", starter_path)

    assert (
        etf_prices.load_target_tickers(explicit_specs=[], limit=0, universe="starter")
        == EXPECTED_TICKERS
    )
