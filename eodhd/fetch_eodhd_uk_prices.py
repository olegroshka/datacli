"""Fetch daily prices for the ``uk_domestic`` lane from EODHD.

Same shape as ``us_extended``: the shared ETF price fetcher runs against this
lane's universe and output files (``register_lanes``). History starts at
2012-01-01 unless ``--from`` is given. The routine refresh prices the live
names only; the first fill and any rebuild pass ``--include-delisted``;
``--register-only`` restricts the run to the issuers named in the UK short
register (the subset WP18 needs first).

Usage:
    uv run python eodhd/fetch_eodhd_uk_prices.py --register-only --include-delisted
    uv run python eodhd/fetch_eodhd_uk_prices.py --include-delisted
    uv run python eodhd/fetch_eodhd_uk_prices.py --tickers BA.LSE

Outputs:
    data/raw/eodhd/uk_domestic/prices_daily.parquet
    data/raw/eodhd/uk_domestic/prices_fetch_state.csv
"""

from __future__ import annotations

import fetch_eodhd_us_etf_prices as base
from fetch_eodhd_uk_universe import SPEC
from register_lanes import TargetLoader, configure_prices, prices_main

PRICES_PATH = SPEC.raw_dir / "prices_daily.parquet"
PRICES_STATE_PATH = SPEC.raw_dir / "prices_fetch_state.csv"
LOADER = TargetLoader(SPEC.tickers_path, SPEC.exchange)
load_target_tickers = LOADER


def take_flags() -> None:
    LOADER.take_flags()


def configure() -> None:
    configure_prices(base, SPEC, LOADER, "eodhd_uk_prices")


def main() -> None:
    prices_main(base, SPEC, LOADER, "eodhd_uk_prices")


if __name__ == "__main__":
    main()
