from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_uk_eu_etf_prices as etf_prices  # type: ignore  # noqa: E402
import fetch_eodhd_uk_eu_etf_universe as etf_universe  # type: ignore  # noqa: E402

EXPECTED_TICKERS = [("VUKE", "LSE"), ("XDAX", "XETRA")]


def test_load_target_tickers_uses_provider_file_by_default(tmp_path: Path) -> None:
    provider_path = tmp_path / "tickers_UK_EU_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "XDAX", "Exchange": "XETRA", "Name": "Xtrackers DAX UCITS ETF"},
            {"Code": "VUKE", "Exchange": "LSE", "Name": "Vanguard FTSE 100 UCITS ETF"},
        ]
    ).to_parquet(provider_path, index=False)

    assert (
        etf_universe.load_target_tickers(
            explicit_specs=[], limit=0, provider_path=provider_path
        )
        == EXPECTED_TICKERS
    )


def test_load_target_tickers_uses_monkeypatched_default_provider_path(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_UK_EU_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "XDAX", "Exchange": "XETRA", "Name": "Xtrackers DAX UCITS ETF"},
            {"Code": "VUKE", "Exchange": "LSE", "Name": "Vanguard FTSE 100 UCITS ETF"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(etf_universe, "ETF_TICKERS_PATH", provider_path)

    assert (
        etf_universe.load_target_tickers(explicit_specs=[], limit=0) == EXPECTED_TICKERS
    )


def test_etf_price_loader_prefers_explicit_specs() -> None:
    assert etf_prices.load_target_tickers(
        explicit_specs=["VUKE.LSE", "XDAX.XETRA"],
        limit=0,
        provider_path=Path("unused.parquet"),
    ) == [("VUKE", "LSE"), ("XDAX", "XETRA")]


def test_etf_price_loader_defaults_to_provider_universe(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_UK_EU_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "XDAX", "Exchange": "XETRA", "Name": "Xtrackers DAX UCITS ETF"},
            {"Code": "VUKE", "Exchange": "LSE", "Name": "Vanguard FTSE 100 UCITS ETF"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(etf_prices, "ETF_TICKERS_PATH", provider_path)

    assert (
        etf_prices.load_target_tickers(
            explicit_specs=[], limit=0, provider_path=provider_path
        )
        == EXPECTED_TICKERS
    )
