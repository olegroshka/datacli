"""Build the starter US ETF universe for the EODHD ETF sleeve.

This first slice keeps ETFs separate from the US common-stock issuer universe and
writes a curated liquid starter basket under `data/raw/eodhd/us_etf/`.

Usage:
    uv run python eodhd/fetch_eodhd_us_etf_universe.py
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pandas as pd
import requests
import _atomic
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_eu_fundamentals import parse_ticker_spec
from fetch_eodhd_us_fundamentals import _ROOT, _api_get, _get_api_key

RAW_DIR = EODHD_RAW_ROOT / "us_etf"
ETF_TICKERS_PATH = RAW_DIR / "tickers_US_ETF.parquet"
STARTER_UNIVERSE_PATH = RAW_DIR / "starter_universe.csv"
TARGET_UNIVERSES = ("provider", "starter")

EODHD_US_EXCHANGE = "US"
DELAY = 0.12

STARTER_ETFS = [
    ("SPY", "US"),
    ("QQQ", "US"),
    ("IWM", "US"),
    ("DIA", "US"),
    ("TLT", "US"),
    ("IEF", "US"),
    ("LQD", "US"),
    ("HYG", "US"),
    ("GLD", "US"),
    ("XLK", "US"),
    ("XLF", "US"),
    ("XLE", "US"),
    ("XLV", "US"),
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_etf_universe")


def fetch_exchange_etfs(
    session: requests.Session, exchange: str = EODHD_US_EXCHANGE
) -> pd.DataFrame:
    data = _api_get(session, f"exchange-symbol-list/{exchange}")
    if not data or not isinstance(data, list):
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "Type" in df.columns:
        df = df[df["Type"].astype(str).str.upper().str.contains("ETF", na=False)].copy()
    time.sleep(DELAY)
    return df


def build_starter_universe(provider_df: pd.DataFrame | None = None) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    provider_lookup: dict[tuple[str, str], dict[str, object]] = {}
    if provider_df is not None and not provider_df.empty:
        for row in provider_df.to_dict(orient="records"):
            normalized_row = {str(key): value for key, value in row.items()}
            code = str(normalized_row.get("Code", "")).strip()
            if code:
                provider_lookup[(code, "US")] = normalized_row

    for ticker, exchange in STARTER_ETFS:
        provider_row = provider_lookup.get((ticker, exchange), {})
        rows.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "name": provider_row.get("Name", ""),
                "provider_type": provider_row.get("Type", "ETF"),
                "provider_exchange": provider_row.get("Exchange", exchange),
                "currency": provider_row.get("Currency", "USD"),
                "isin": provider_row.get("Isin", ""),
                "starter": True,
            }
        )
    return pd.DataFrame(rows).sort_values(["exchange", "ticker"]).reset_index(drop=True)


def load_provider_universe(provider_path: Path | None = None) -> pd.DataFrame:
    resolved_provider_path = (
        ETF_TICKERS_PATH if provider_path is None else provider_path
    )
    if not resolved_provider_path.exists():
        raise RuntimeError(
            f"Provider ETF universe file not found: {resolved_provider_path}"
        )
    provider_df = pd.read_parquet(resolved_provider_path)
    if "Code" not in provider_df.columns:
        raise RuntimeError(
            f"Provider ETF universe file is missing required column 'Code': {resolved_provider_path}"
        )
    normalized = pd.DataFrame(
        {
            "ticker": provider_df["Code"].astype(str).str.strip(),
            "exchange": EODHD_US_EXCHANGE,
        }
    )
    normalized = (
        normalized[normalized["ticker"] != ""]
        .drop_duplicates()
        .sort_values(["exchange", "ticker"])
        .reset_index(drop=True)
    )
    return normalized


def load_starter_universe(starter_path: Path | None = None) -> pd.DataFrame:
    resolved_starter_path = (
        STARTER_UNIVERSE_PATH if starter_path is None else starter_path
    )
    if not resolved_starter_path.exists():
        raise RuntimeError(
            f"Starter ETF universe file not found: {resolved_starter_path}"
        )
    starter = pd.read_csv(resolved_starter_path, usecols=["ticker", "exchange"])
    return (
        starter.drop_duplicates()
        .sort_values(["exchange", "ticker"])
        .reset_index(drop=True)
    )


def load_target_tickers(
    *,
    explicit_specs: list[str],
    limit: int = 0,
    universe: str = "provider",
    provider_path: Path | None = None,
    starter_path: Path | None = None,
) -> list[tuple[str, str]]:
    if explicit_specs:
        tickers = [parse_ticker_spec(value) for value in explicit_specs]
    else:
        if universe not in TARGET_UNIVERSES:
            raise ValueError(
                f"Unknown ETF universe '{universe}'. Expected one of: {', '.join(TARGET_UNIVERSES)}"
            )
        target_df = (
            load_provider_universe(provider_path=provider_path)
            if universe == "provider"
            else load_starter_universe(starter_path=starter_path)
        )
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

    provider_df = fetch_exchange_etfs(session)
    if not provider_df.empty:
        _atomic.to_parquet(provider_df, ETF_TICKERS_PATH, index=False)
        log.info("Saved provider ETF list: %d rows", len(provider_df))
    else:
        log.warning(
            "Provider ETF list fetch returned no rows; continuing with starter basket only"
        )

    starter_df = build_starter_universe(provider_df)
    _atomic.to_csv(starter_df, STARTER_UNIVERSE_PATH, index=False)
    log.info("Saved ETF starter universe: %d rows", len(starter_df))
    print(starter_df.to_string(index=False))


if __name__ == "__main__":
    main()
