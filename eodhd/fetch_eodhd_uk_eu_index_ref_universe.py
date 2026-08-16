"""Fetch the UK/EU-focused index / benchmark reference universe from EODHD `INDX`.

This sleeve filters the global `INDX` exchange to UK/EU-relevant benchmark/index
symbols using provider country tags plus a regional benchmark name/code pattern.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path

import pandas as pd
import requests
from fetch_eodhd_eu_fundamentals import RAW_DIR as UK_EU_COMMON_RAW_DIR
from fetch_eodhd_eu_fundamentals import (
    _api_get,
    _get_api_key,
    parse_ticker_spec,
)

RAW_DIR = UK_EU_COMMON_RAW_DIR.parent / "uk_eu_index_ref"
INDEX_TICKERS_PATH = RAW_DIR / "tickers_INDX_UK_EU.parquet"
EODHD_INDEX_EXCHANGE = "INDX"
DELAY = 0.12

UK_EU_COUNTRIES = {
    "UK",
    "UNITED KINGDOM",
    "GERMANY",
    "FRANCE",
    "NETHERLANDS",
    "SWITZERLAND",
    "ITALY",
    "SPAIN",
    "SWEDEN",
    "NORWAY",
    "FINLAND",
    "DENMARK",
    "AUSTRIA",
}
UK_EU_INDEX_PATTERN = re.compile(
    r"FTSE|DAX|MDAX|SDAX|TECDAX|CAC|EURO STOXX|STOXX EUROPE|AEX|SMI|IBEX|FTSE MIB|OMX|OBX|ATX|CBOE UK|BEL 20",
    re.IGNORECASE,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_uk_eu_index_ref_universe")


def fetch_exchange_indices(
    session: requests.Session, exchange: str = EODHD_INDEX_EXCHANGE
) -> pd.DataFrame:
    data = _api_get(session, f"exchange-symbol-list/{exchange}")
    if not data or not isinstance(data, list):
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "Type" in df.columns:
        df = df[
            df["Type"].astype(str).str.upper().str.contains("INDEX", na=False)
        ].copy()
    if df.empty:
        return df
    country_mask = (
        df["Country"].astype(str).str.upper().isin(UK_EU_COUNTRIES)
        if "Country" in df.columns
        else pd.Series([False] * len(df))
    )
    name_mask = (
        df["Name"].astype(str).str.contains(UK_EU_INDEX_PATTERN, na=False)
        if "Name" in df.columns
        else pd.Series([False] * len(df))
    )
    code_mask = (
        df["Code"].astype(str).str.contains(UK_EU_INDEX_PATTERN, na=False)
        if "Code" in df.columns
        else pd.Series([False] * len(df))
    )
    filtered = df[country_mask | name_mask | code_mask].copy()
    time.sleep(DELAY)
    return filtered


def load_provider_universe(provider_path: Path | None = None) -> pd.DataFrame:
    resolved_provider_path = (
        INDEX_TICKERS_PATH if provider_path is None else provider_path
    )
    if not resolved_provider_path.exists():
        raise RuntimeError(
            f"UK/EU index reference universe file not found: {resolved_provider_path}"
        )
    provider_df = pd.read_parquet(resolved_provider_path)
    if "Code" not in provider_df.columns:
        raise RuntimeError(
            f"UK/EU index reference universe file is missing required column 'Code': {resolved_provider_path}"
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
        raise RuntimeError(
            "Provider UK/EU index / benchmark universe fetch returned no rows"
        )

    provider_df = (
        provider_df.drop_duplicates(subset=["Code"])
        .sort_values(["Code"])
        .reset_index(drop=True)
    )
    _atomic.to_parquet(provider_df, INDEX_TICKERS_PATH, index=False)
    log.info("Saved provider UK/EU index / benchmark list: %d rows", len(provider_df))
    if "Country" in provider_df.columns:
        print("Country counts:")
        print(
            provider_df["Country"].fillna("Unknown").value_counts().head(15).to_string()
        )


if __name__ == "__main__":
    main()
