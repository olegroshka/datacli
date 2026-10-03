"""Fetch split history for the ``us_extended`` lane from EODHD.

Runs the ETF split fetcher against the ``us_extended`` universe and output
files (see ``fetch_eodhd_us_extended_prices.py`` for why).

Usage:
    uv run python eodhd/fetch_eodhd_us_extended_splits.py

Outputs:
    data/raw/eodhd/us_extended/splits_history.parquet
    data/raw/eodhd/us_extended/splits_fetch_state.csv
    data/raw/eodhd/us_extended/splits_fetch_audit.csv
"""

from __future__ import annotations

import logging
import sys

import fetch_eodhd_us_etf_splits as base
from fetch_eodhd_us_extended_universe import RAW_DIR, TICKERS_PATH

SPLITS_PATH = RAW_DIR / "splits_history.parquet"
SPLITS_AUDIT_PATH = RAW_DIR / "splits_fetch_audit.csv"
SPLITS_STATE_PATH = RAW_DIR / "splits_fetch_state.csv"


def load_target_tickers(*, explicit_specs: list[str], limit: int = 0, **_ignored) -> list[tuple[str, str]]:
    """The lane's universe; delisted names only with ``--include-delisted``.

    A delisted symbol's history never grows, so the routine refresh skips it
    and the daily cost stays at the live names. The first fill, and any
    rebuild, passes ``--include-delisted``.
    """
    if explicit_specs:
        from fetch_eodhd_eu_fundamentals import parse_ticker_spec

        return [parse_ticker_spec(value) for value in explicit_specs][: limit or None]
    import pandas as pd

    universe = pd.read_parquet(TICKERS_PATH)
    if not include_delisted() and "delisted" in universe.columns:
        universe = universe[~universe["delisted"].astype(bool)]
    tickers = [(str(code).strip(), "US") for code in universe["Code"] if str(code).strip()]
    return tickers[: limit or None]


def include_delisted() -> bool:
    if "--include-delisted" in sys.argv:
        sys.argv.remove("--include-delisted")
        return True
    return False


def configure() -> None:
    """Point the shared fetcher at this lane's universe and outputs."""
    base.SPLITS_PATH = SPLITS_PATH
    base.SPLITS_AUDIT_PATH = SPLITS_AUDIT_PATH
    base.SPLITS_STATE_PATH = SPLITS_STATE_PATH
    base.ETF_TICKERS_PATH = TICKERS_PATH
    base.load_target_tickers = load_target_tickers
    base.log = logging.getLogger("eodhd_us_extended_splits")


def main() -> None:
    configure()
    base.main()


if __name__ == "__main__":
    main()
