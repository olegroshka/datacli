"""Fetch daily prices for the ``uk_domestic`` lane from EODHD.

Same shape as ``us_extended``: the shared ETF price fetcher runs against this
lane's universe and output files. History starts at ``DEFAULT_FROM`` unless
``--from`` is given. The routine refresh prices the live names only; the
first fill and any rebuild pass ``--include-delisted``; ``--register-only``
restricts the run to the issuers named in the UK short register (the subset
WP18 needs first).

Usage:
    uv run python eodhd/fetch_eodhd_uk_prices.py --register-only --include-delisted
    uv run python eodhd/fetch_eodhd_uk_prices.py --include-delisted
    uv run python eodhd/fetch_eodhd_uk_prices.py --tickers BA.LSE

Outputs:
    data/raw/eodhd/uk_domestic/prices_daily.parquet
    data/raw/eodhd/uk_domestic/prices_fetch_state.csv
"""

from __future__ import annotations

import logging
import sys

import fetch_eodhd_us_etf_prices as base
from fetch_eodhd_uk_universe import DEFAULT_FROM, EODHD_EXCHANGE, RAW_DIR, TICKERS_PATH

PRICES_PATH = RAW_DIR / "prices_daily.parquet"
PRICES_STATE_PATH = RAW_DIR / "prices_fetch_state.csv"


#: The lane's own flags, taken off ``sys.argv`` before the shared parser sees it.
INCLUDE_DELISTED = False
REGISTER_ONLY = False


def _take_flag(flag: str) -> bool:
    if flag in sys.argv:
        sys.argv.remove(flag)
        return True
    return False


def take_flags() -> None:
    global INCLUDE_DELISTED, REGISTER_ONLY
    INCLUDE_DELISTED = _take_flag("--include-delisted") or INCLUDE_DELISTED
    REGISTER_ONLY = _take_flag("--register-only") or REGISTER_ONLY


def load_target_tickers(*, explicit_specs: list[str], limit: int = 0, **_ignored) -> list[tuple[str, str]]:
    """The lane's universe; delisted names only with ``--include-delisted``, the register's issuers only with ``--register-only``."""
    if explicit_specs:
        from fetch_eodhd_eu_fundamentals import parse_ticker_spec

        return [parse_ticker_spec(value) for value in explicit_specs][: limit or None]
    import pandas as pd

    take_flags()
    universe = pd.read_parquet(TICKERS_PATH)
    if not INCLUDE_DELISTED and "delisted" in universe.columns:
        universe = universe[~universe["delisted"].astype(bool)]
    if REGISTER_ONLY and "in_register" in universe.columns:
        universe = universe[universe["in_register"].astype(bool)]
    tickers = [(str(code).strip(), EODHD_EXCHANGE) for code in universe["Code"] if str(code).strip()]
    return tickers[: limit or None]


def configure() -> None:
    """Point the shared fetcher at this lane's universe and outputs."""
    base.PRICES_PATH = PRICES_PATH
    base.PRICES_STATE_PATH = PRICES_STATE_PATH
    base.ETF_TICKERS_PATH = TICKERS_PATH
    base.load_target_tickers = load_target_tickers
    base.log = logging.getLogger("eodhd_uk_prices")


def main() -> None:
    configure()
    take_flags()  # before the shared parser rejects them
    if not any(arg == "--from" or arg.startswith("--from=") for arg in sys.argv[1:]):
        sys.argv += ["--from", DEFAULT_FROM]
    base.main()


if __name__ == "__main__":
    main()
