"""Build the ``uk_domestic`` universe: the LSE's domestic common stocks, listed and delisted.

The ``uk_eu`` lane prices the LSE's international listings (the ``0xxx``
codes); the public short register (``registers``, DD-003) names the UK's
domestic issuers, most of which the lane does not hold, and many of which
have been delisted since 2012. The universe is every LSE common stock quoted
in sterling (GBX or GBP) on the provider's active and delisted symbol lists
(two API calls), plus any other LSE common stock whose ISIN appears in the
register; ``in_register`` marks the latter set so the first fill can price
the register's issuers before the rest.

Usage:
    uv run python eodhd/fetch_eodhd_uk_universe.py --plan   # counts only
    uv run python eodhd/fetch_eodhd_uk_universe.py

Outputs:
    data/raw/eodhd/uk_domestic/tickers_UK.parquet
    data/raw/eodhd/uk_domestic/register_unmatched.csv   (register ISINs no provider list knows)
"""

from __future__ import annotations

import argparse
import logging
from typing import Any, Iterable, Mapping

import _atomic
import pandas as pd
import requests
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_us_fundamentals import _api_get, _get_api_key

RAW_DIR = EODHD_RAW_ROOT / "uk_domestic"
TICKERS_PATH = RAW_DIR / "tickers_UK.parquet"
UNMATCHED_PATH = RAW_DIR / "register_unmatched.csv"
EODHD_EXCHANGE = "LSE"
#: History start for this lane: the register begins in late 2012.
DEFAULT_FROM = "2012-01-01"
COLUMNS = ["Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin"]
KEEP_TYPES = frozenset({"common stock"})
DOMESTIC_CURRENCIES = frozenset({"GBX", "GBP"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_uk_universe")


def select_universe(
    active: Iterable[Mapping[str, Any]],
    delisted: Iterable[Mapping[str, Any]],
    register_isins: set[str],
) -> tuple[pd.DataFrame, set[str]]:
    """The lane's rows and the register ISINs no list knows.

    Keeps common stocks quoted in sterling, plus common stocks of any currency
    whose ISIN is in the register. A code present in both lists is taken from
    the active one.
    """
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, is_delisted in ((active, False), (delisted, True)):
        for raw in source:
            code = str(raw.get("Code", "")).strip()
            if not code or code in seen:
                continue
            kind = str(raw.get("Type", "")).strip().lower()
            if kind not in KEEP_TYPES:
                continue
            currency = str(raw.get("Currency", "")).strip().upper()
            isin = str(raw.get("Isin", "") or "").strip().upper()
            in_register = bool(isin) and isin in register_isins
            if currency not in DOMESTIC_CURRENCIES and not in_register:
                continue
            seen.add(code)
            row = {column: raw.get(column, "") for column in COLUMNS}
            row.update(delisted=is_delisted, in_register=in_register)
            rows.append(row)
    frame = pd.DataFrame(rows, columns=COLUMNS + ["delisted", "in_register"])
    frame = frame.sort_values(["in_register", "delisted", "Code"], ascending=[False, True, True]).reset_index(drop=True)
    known = set(frame["Isin"].astype(str).str.upper())
    return frame, {i for i in register_isins if i not in known}


def register_isins(con: Any) -> set[str]:
    """The UK register's ISINs, empty when the register is not stored."""
    try:
        rows = con.execute("SELECT DISTINCT isin FROM short_register WHERE market = 'uk' AND isin IS NOT NULL").fetchall()
    except Exception:  # noqa: BLE001 (the view is optional)
        return set()
    return {str(r[0]).strip().upper() for r in rows}


def fetch_symbol_list(session: requests.Session, *, delisted: bool) -> list[dict]:
    data = _api_get(session, f"exchange-symbol-list/{EODHD_EXCHANGE}", {"delisted": "1"} if delisted else None)
    if not data or not isinstance(data, list):
        raise RuntimeError(f"empty {'delisted' if delisted else 'active'} {EODHD_EXCHANGE} symbol list from the provider")
    return [row for row in data if row.get("Code")]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", action="store_true", help="print the counts and write nothing")
    args = parser.parse_args()

    import explore_eodhd

    wanted = register_isins(explore_eodhd.connect())
    log.info("Register ISINs (uk): %d", len(wanted))
    session = requests.Session()
    session.params = {"api_token": _get_api_key()}
    active = fetch_symbol_list(session, delisted=False)
    delisted = fetch_symbol_list(session, delisted=True)
    log.info("Provider lists: %d active, %d delisted", len(active), len(delisted))
    universe, unmatched = select_universe(active, delisted, wanted)
    log.info(
        "Universe %d (%d delisted, %d in the register); register ISINs unmatched %d",
        len(universe), int(universe["delisted"].sum()), int(universe["in_register"].sum()), len(unmatched),
    )
    log.info("By currency: %s", universe["Currency"].value_counts().head(6).to_dict())
    log.info("First fill costs about %d price calls (%d for the register's issuers)", len(universe), int(universe["in_register"].sum()))
    if args.plan:
        log.info("--plan: nothing written")
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(universe, TICKERS_PATH, index=False)
    log.info("Wrote %s", TICKERS_PATH)
    _atomic.to_csv(pd.DataFrame({"isin": sorted(unmatched)}), UNMATCHED_PATH, index=False)
    log.info("Wrote %s (%d ISINs)", UNMATCHED_PATH, len(unmatched))


if __name__ == "__main__":
    main()
