"""Fetch fundamentals (shares outstanding above all) for the ``us_extended`` lane.

Runs the US common-stock fundamentals fetcher against this lane's directory
and universe. The fetcher reads ``tickers_US.parquet`` from its lane
directory, so that file is derived here from the lane universe
(``tickers_US_EXT.parquet``): common stocks only, and delisted names only
with ``--include-delisted`` (a first fill; a delisted name's filings never
change, so the routine ``--update`` leaves them alone).

Usage:
    uv run python eodhd/fetch_eodhd_us_extended_fundamentals.py --include-delisted   # first fill
    uv run python eodhd/fetch_eodhd_us_extended_fundamentals.py --update             # routine

Outputs (under data/raw/eodhd/us_extended/): the same tables as us_common,
``fundamentals_quarterly.parquet``, ``firm_metadata.parquet``,
``outstanding_shares_quarterly.parquet`` and the other section snapshots,
plus ``fundamentals_fetch_state.csv``.
"""

from __future__ import annotations

import logging
import sys

import _atomic
import fetch_eodhd_us_fundamentals as base
import pandas as pd
from fetch_eodhd_us_extended_universe import RAW_DIR, TICKERS_PATH

FUND_TICKERS_PATH = RAW_DIR / "tickers_US.parquet"
UNIVERSE_COLUMNS = ["Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin"]


def include_delisted() -> bool:
    if "--include-delisted" in sys.argv:
        sys.argv.remove("--include-delisted")
        return True
    return False


def write_fetch_universe(*, delisted: bool) -> int:
    """``tickers_US.parquet`` for the fetcher: the lane's common stocks, live or all."""
    universe = pd.read_parquet(TICKERS_PATH)
    universe = universe[universe["Type"].astype(str).str.lower() == "common stock"]
    if not delisted and "delisted" in universe.columns:
        universe = universe[~universe["delisted"].astype(bool)]
    frame = universe[[c for c in UNIVERSE_COLUMNS if c in universe.columns]].reset_index(drop=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(frame, FUND_TICKERS_PATH, index=False)
    return len(frame)


def configure() -> None:
    """Point the shared fetcher at this lane's directory and outputs."""
    base.RAW_DIR = RAW_DIR
    base.RAW_CACHE_DIR = RAW_DIR / "cache" / "fundamentals"
    base.SECTION_OUTPUT_SPECS = base._build_section_output_specs(raw_dir=RAW_DIR)
    base.log = logging.getLogger("eodhd_us_extended_fundamentals")


def main() -> None:
    delisted = include_delisted()
    count = write_fetch_universe(delisted=delisted)
    logging.getLogger("eodhd_us_extended_fundamentals").info(
        "fundamentals universe: %d common stocks (%s)", count,
        "delisted included" if delisted else "live names only",
    )
    configure()
    base.main()


if __name__ == "__main__":
    main()
