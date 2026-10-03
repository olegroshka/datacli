"""Build the ``uk_domestic`` universe: the LSE's domestic common stocks, listed and delisted.

The ``uk_eu`` lane prices the LSE's international listings (the ``0xxx``
codes); the public short register (``registers``, DD-003) names the UK's
domestic issuers, most of which the lane does not hold, and many of which
have been delisted since 2012. The universe is every LSE common stock quoted
in sterling (GBX or GBP) on the provider's active and delisted symbol lists
(two API calls), plus any other LSE common stock whose ISIN appears in the
register; ``in_register`` marks the latter set so the first fill can price
the register's issuers before the rest (``register_lanes``).

Usage:
    uv run python eodhd/fetch_eodhd_uk_universe.py --plan   # counts only
    uv run python eodhd/fetch_eodhd_uk_universe.py

Outputs:
    data/raw/eodhd/uk_domestic/tickers_UK.parquet
    data/raw/eodhd/uk_domestic/register_unmatched.csv   (register ISINs no provider list knows)
"""

from __future__ import annotations

import logging
from typing import Any, Iterable, Mapping

import pandas as pd

from register_lanes import COLUMNS, KEEP_TYPES, LaneSpec, universe_main  # noqa: F401
from register_lanes import register_isins as _register_isins
from register_lanes import select_universe as _select_universe

SPEC = LaneSpec(name="uk_domestic", market="uk", exchange="LSE", currencies=frozenset({"GBX", "GBP"}), tickers_file="tickers_UK.parquet")
RAW_DIR = SPEC.raw_dir
TICKERS_PATH = SPEC.tickers_path
UNMATCHED_PATH = SPEC.unmatched_path
EODHD_EXCHANGE = SPEC.exchange
DEFAULT_FROM = SPEC.default_from
DOMESTIC_CURRENCIES = SPEC.currencies

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("eodhd_uk_universe")


def select_universe(active: Iterable[Mapping[str, Any]], delisted: Iterable[Mapping[str, Any]], register_isins: set[str]) -> tuple[pd.DataFrame, set[str]]:
    return _select_universe(active, delisted, register_isins, currencies=SPEC.currencies)


def register_isins(con: Any) -> set[str]:
    return _register_isins(con, SPEC.market)


def main() -> None:
    universe_main(SPEC, log, __doc__ or "")


if __name__ == "__main__":
    main()
