"""Fetch daily prices for the ``fr_domestic`` lane from EODHD (see ``fetch_eodhd_uk_prices``).

Usage:
    uv run python eodhd/fetch_eodhd_fr_prices.py --register-only --include-delisted
    uv run python eodhd/fetch_eodhd_fr_prices.py --include-delisted

Outputs:
    data/raw/eodhd/fr_domestic/prices_daily.parquet
    data/raw/eodhd/fr_domestic/prices_fetch_state.csv
"""

from __future__ import annotations

import fetch_eodhd_us_etf_prices as base
from fetch_eodhd_fr_universe import SPEC
from register_lanes import TargetLoader, configure_prices, prices_main

PRICES_PATH = SPEC.raw_dir / "prices_daily.parquet"
PRICES_STATE_PATH = SPEC.raw_dir / "prices_fetch_state.csv"
LOADER = TargetLoader(SPEC.tickers_path, SPEC.exchange)
load_target_tickers = LOADER


def take_flags() -> None:
    LOADER.take_flags()


def configure() -> None:
    configure_prices(base, SPEC, LOADER, "eodhd_fr_prices")


def main() -> None:
    prices_main(base, SPEC, LOADER, "eodhd_fr_prices")


if __name__ == "__main__":
    main()
