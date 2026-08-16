"""Fetch the UK/EU ETF universe across the existing target European exchanges.

This sleeve is kept separate from the UK/EU common-stock lane and captures only
provider rows whose `Type` contains `ETF` across the established UK/EU exchange
set from `fetch_eodhd_eu_fundamentals.py`.

Usage:
    uv run python eodhd/fetch_eodhd_uk_eu_etf_universe.py
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import requests
from fetch_eodhd_eu_fundamentals import RAW_DIR as UK_EU_COMMON_RAW_DIR
from fetch_eodhd_eu_fundamentals import (
    TARGET_EXCHANGES,
    _api_get,
    _get_api_key,
    parse_ticker_spec,
)

RAW_DIR = UK_EU_COMMON_RAW_DIR.parent / "uk_eu_etf"
ETF_TICKERS_PATH = RAW_DIR / "tickers_UK_EU_ETF.parquet"
DELAY = 0.12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_uk_eu_etf_universe")


def fetch_exchange_etfs(session: requests.Session, exchange: str) -> pd.DataFrame:
    data = _api_get(session, f"exchange-symbol-list/{exchange}")
    if not data or not isinstance(data, list):
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "Type" in df.columns:
        df = df[df["Type"].astype(str).str.upper().str.contains("ETF", na=False)].copy()
    if not df.empty:
        df["source_exchange"] = exchange
    time.sleep(DELAY)
    return df


def load_provider_universe(provider_path: Path | None = None) -> pd.DataFrame:
    resolved_provider_path = (
        ETF_TICKERS_PATH if provider_path is None else provider_path
    )
    if not resolved_provider_path.exists():
        raise RuntimeError(
            f"UK/EU ETF universe file not found: {resolved_provider_path}"
        )
    provider_df = pd.read_parquet(resolved_provider_path)
    required = {"Code", "Exchange"}
    if not required.issubset(provider_df.columns):
        raise RuntimeError(
            f"UK/EU ETF universe file is missing required columns {sorted(required)}: {resolved_provider_path}"
        )
    normalized = pd.DataFrame(
        {
            "ticker": provider_df["Code"].astype(str).str.strip(),
            "exchange": provider_df["Exchange"].astype(str).str.strip(),
        }
    )
    return (
        normalized[(normalized["ticker"] != "") & (normalized["exchange"] != "")]
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

    frames: list[pd.DataFrame] = []
    for exchange in TARGET_EXCHANGES:
        df = fetch_exchange_etfs(session, exchange)
        if df.empty:
            log.warning("No ETF rows returned for %s", exchange)
            continue
        frames.append(df)
        log.info("%s ETF rows: %d", exchange, len(df))

    if not frames:
        raise RuntimeError("Provider UK/EU ETF universe fetch returned no rows")

    provider_df = pd.concat(frames, ignore_index=True)
    provider_df = (
        provider_df.drop_duplicates(subset=["Code", "Exchange"])
        .sort_values(["Exchange", "Code"])
        .reset_index(drop=True)
    )
    _atomic.to_parquet(provider_df, ETF_TICKERS_PATH, index=False)
    log.info("Saved provider UK/EU ETF list: %d rows", len(provider_df))

    exchange_counts = (
        provider_df["Exchange"].fillna("Unknown").value_counts().sort_index()
    )
    print("Exchange counts:")
    print(exchange_counts.to_string())


if __name__ == "__main__":
    main()
