from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_uk_eu_index_ref_prices as index_prices  # type: ignore  # noqa: E402
import fetch_eodhd_uk_eu_index_ref_universe as index_universe  # type: ignore  # noqa: E402

EXPECTED_TICKERS = [("AEX", "INDX"), ("GDAXI", "INDX")]


def test_index_universe_loader_uses_provider_file_by_default(tmp_path: Path) -> None:
    provider_path = tmp_path / "tickers_INDX_UK_EU.parquet"
    pd.DataFrame(
        [
            {"Code": "GDAXI", "Name": "DAX Index"},
            {"Code": "AEX", "Name": "AEX Amsterdam Index"},
        ]
    ).to_parquet(provider_path, index=False)

    assert (
        index_universe.load_target_tickers(
            explicit_specs=[], limit=0, provider_path=provider_path
        )
        == EXPECTED_TICKERS
    )


def test_index_universe_loader_uses_monkeypatched_default_provider_path(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_INDX_UK_EU.parquet"
    pd.DataFrame(
        [
            {"Code": "GDAXI", "Name": "DAX Index"},
            {"Code": "AEX", "Name": "AEX Amsterdam Index"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(index_universe, "INDEX_TICKERS_PATH", provider_path)

    assert (
        index_universe.load_target_tickers(explicit_specs=[], limit=0)
        == EXPECTED_TICKERS
    )


def test_index_price_loader_prefers_explicit_specs() -> None:
    assert index_prices.load_target_tickers(
        explicit_specs=["GDAXI.INDX", "AEX.INDX"],
        limit=0,
        provider_path=Path("unused.parquet"),
    ) == [("GDAXI", "INDX"), ("AEX", "INDX")]


def test_index_price_loader_defaults_to_provider_universe(
    tmp_path: Path, monkeypatch
) -> None:
    provider_path = tmp_path / "tickers_INDX_UK_EU.parquet"
    pd.DataFrame(
        [
            {"Code": "GDAXI", "Name": "DAX Index"},
            {"Code": "AEX", "Name": "AEX Amsterdam Index"},
        ]
    ).to_parquet(provider_path, index=False)
    monkeypatch.setattr(index_prices, "INDEX_TICKERS_PATH", provider_path)

    assert (
        index_prices.load_target_tickers(
            explicit_specs=[], limit=0, provider_path=provider_path
        )
        == EXPECTED_TICKERS
    )
