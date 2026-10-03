"""Build the ``fr_domestic`` universe: Euronext Paris's common stocks, listed and delisted.

The AMF's register (``registers``, market ``fr``) names French issuers; the
``uk_eu`` lane holds 40 Paris tickers. The universe is every common stock on
the provider's active and delisted Paris lists quoted in euros, plus any
Paris common stock whose ISIN the register names (``in_register``), so the
first fill prices the register's issuers before the rest (``register_lanes``).

Usage:
    uv run python eodhd/fetch_eodhd_fr_universe.py --plan   # counts only
    uv run python eodhd/fetch_eodhd_fr_universe.py

Outputs:
    data/raw/eodhd/fr_domestic/tickers_FR.parquet
    data/raw/eodhd/fr_domestic/register_unmatched.csv
"""

from __future__ import annotations

import logging

from register_lanes import LaneSpec, universe_main

SPEC = LaneSpec(name="fr_domestic", market="fr", exchange="PA", currencies=frozenset({"EUR"}), tickers_file="tickers_FR.parquet")
RAW_DIR = SPEC.raw_dir
TICKERS_PATH = SPEC.tickers_path
DEFAULT_FROM = SPEC.default_from

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("eodhd_fr_universe")


def main() -> None:
    universe_main(SPEC, log, __doc__ or "")


if __name__ == "__main__":
    main()
