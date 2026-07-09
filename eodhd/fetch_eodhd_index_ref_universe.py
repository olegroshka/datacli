"""Fetch the EODHD index / benchmark reference universe.

This sleeve is separate from the US common-stock and ETF lanes because it holds
provider-maintained reference indices from the dedicated EODHD `INDX` exchange.

Usage:
    uv run python eodhd/fetch_eodhd_index_ref_universe.py
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import requests

from fetch_eodhd_eu_fundamentals import parse_ticker_spec
from fetch_eodhd_us_fundamentals import _ROOT, _api_get, _get_api_key
from _datadir import EODHD_RAW_ROOT

RAW_DIR = EODHD_RAW_ROOT / "index_ref"
INDEX_TICKERS_PATH = RAW_DIR / "tickers_INDX.parquet"

EODHD_INDEX_EXCHANGE = "INDX"
DELAY = 0.12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_index_ref_universe")


def fetch_exchange_indices(
    session: requests.Session,
    exchange: str = EODHD_INDEX_EXCHANGE,
) -> pd.DataFrame:
    data = _api_get(session, f"exchange-symbol-list/{exchange}")
    if not data or not isinstance(data, list):
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "Type" in df.columns:
        df = df[
            df["Type"].astype(str).str.upper().str.contains("INDEX", na=False)
        ].copy()
    time.sleep(DELAY)
    return df


def load_provider_universe(provider_path: Path | None = None) -> pd.DataFrame:
    resolved_provider_path = (
        INDEX_TICKERS_PATH if provider_path is None else provider_path
    )
    if not resolved_provider_path.exists():
        raise RuntimeError(
            f"Index reference universe file not found: {resolved_provider_path}"
        )
    provider_df = pd.read_parquet(resolved_provider_path)
    if "Code" not in provider_df.columns:
        raise RuntimeError(
            f"Index reference universe file is missing required column 'Code': {resolved_provider_path}"
        )
    normalized = pd.DataFrame(
        {
            "ticker": provider_df["Code"].astype(str).str.strip(),
            "exchange": EODHD_INDEX_EXCHANGE,
        }
    )
    return (
        normalized[normalized["ticker"] != ""]
        .drop_duplicates()
        .sort_values(["exchange", "ticker"])
        .reset_index(drop=True)
    )


def load_target_tickers(
    *,
    explicit_specs: list[str],
    limit: int = 0,
    provider_path: Path | None = None,
) -> list[tuple[str, str]]:
    if explicit_specs:
        tickers = [parse_ticker_spec(value) for value in explicit_specs]
    else:
        target_df = load_provider_universe(provider_path=provider_path)
        tickers = [
            tuple(row)
            for row in target_df[["ticker", "exchange"]].itertuples(
                index=False, name=None
            )
        ]
    if limit > 0:
        tickers = tickers[:limit]
    return tickers


def main() -> None:
    api_key = _get_api_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.params = {"api_token": api_key}
    session.headers.update({"Accept": "application/json"})

    provider_df = fetch_exchange_indices(session)
    if provider_df.empty:
        raise RuntimeError("Provider index / benchmark universe fetch returned no rows")

    provider_df.to_parquet(INDEX_TICKERS_PATH, index=False)
    log.info("Saved provider index / benchmark list: %d rows", len(provider_df))
    if "Country" in provider_df.columns:
        top_countries = provider_df["Country"].fillna("Unknown").value_counts().head(10)
        print("Top countries:")
        print(top_countries.to_string())


if __name__ == "__main__":
    main()
