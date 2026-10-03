"""Build the ``us_extended`` universe: shorted US names the other lanes do not price.

``us_common`` prices only the names whose fundamentals qualify and ``us_etf``
only ETFs, while FINRA short interest lists every exchange-listed security.
The names in between (smaller companies, ADRs, funds, and everything that has
since been delisted) are needed for any return test on positioning data: a
universe of today's survivors drops exactly the heavily shorted names that
failed or were acquired (docs/samrt-money-flow-dataset-initiative/OQ-005).

The universe is every listed common symbol in ``finra_short_interest`` that
has no price series in ``us_common`` or ``us_etf``, matched to an EODHD code
through the provider's active and delisted US symbol lists (two API calls).

Usage:
    uv run python eodhd/fetch_eodhd_us_extended_universe.py --plan   # counts only
    uv run python eodhd/fetch_eodhd_us_extended_universe.py

Outputs:
    data/raw/eodhd/us_extended/tickers_US_EXT.parquet
    data/raw/eodhd/us_extended/unmatched_symbols.csv   (symbols no provider list knows)
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from typing import Any, Mapping

import _atomic
import pandas as pd
import requests
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_us_fundamentals import _api_get, _get_api_key

RAW_DIR = EODHD_RAW_ROOT / "us_extended"
TICKERS_PATH = RAW_DIR / "tickers_US_EXT.parquet"
#: Short interest symbols no provider list knows, kept for `qc` and the record.
UNMATCHED_PATH = RAW_DIR / "unmatched_symbols.csv"
EODHD_US_EXCHANGE = "US"
PRICED_LANES = ("us_common", "us_etf")
#: History start for this lane: a year of warm-up before short interest begins.
DEFAULT_FROM = "2017-01-01"
#: A symbol still reporting within this many days of the newest report is alive.
ALIVE_WINDOW_DAYS = 45
#: The provider's suffix for a delisted symbol whose ticker was reused.
REUSED_SUFFIX = "_old"
COLUMNS = ["Code", "Name", "Country", "Exchange", "Currency", "Type", "Isin"]
#: Provider types kept. Short interest's "common" label comes from the issue
#: name, so units, warrants, preferreds and notes slip through; they are dropped.
KEEP_TYPES = frozenset({"common stock", "etf", "fund"})

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_extended_universe")


def match_symbols(
    wanted: pd.DataFrame,
    active: Mapping[str, Mapping[str, Any]],
    delisted: Mapping[str, Mapping[str, Any]],
    *,
    latest: date,
) -> tuple[pd.DataFrame, list[str]]:
    """Map short interest symbols to provider codes.

    ``wanted`` has ``code``, ``si_first``, ``si_last``. A symbol still reporting
    is looked up in the active list first; one that stopped reporting in the
    delisted list first (then under the reused-ticker suffix), because an
    active symbol with the same code may be a different, later issuer. Returns
    the universe frame and the symbols no list knows.
    """
    rows: list[dict[str, Any]] = []
    unmatched: list[str] = []
    cutoff = latest - timedelta(days=ALIVE_WINDOW_DAYS)
    for code, first, last in wanted[["code", "si_first", "si_last"]].itertuples(
        index=False, name=None
    ):
        alive = last >= cutoff
        reused = f"{code}{REUSED_SUFFIX}"
        if alive and code in active:
            hit, is_delisted = active[code], False
        elif code in delisted:
            hit, is_delisted = delisted[code], True
        elif reused in delisted:
            hit, is_delisted = delisted[reused], True
        elif code in active:
            hit, is_delisted = active[code], False
        else:
            unmatched.append(code)
            continue
        row = {column: hit.get(column, "") for column in COLUMNS}
        row.update(delisted=is_delisted, si_symbol=code, si_first=first, si_last=last)
        rows.append(row)
    frame = pd.DataFrame(
        rows, columns=COLUMNS + ["delisted", "si_symbol", "si_first", "si_last"]
    )
    frame = frame.drop_duplicates("Code").sort_values("Code").reset_index(drop=True)
    return frame, unmatched


def wanted_symbols(con: Any) -> tuple[pd.DataFrame, date]:
    """Listed common short interest symbols with no ``us_common`` / ``us_etf`` prices."""
    lanes = ", ".join(f"'{lane}'" for lane in PRICED_LANES)
    frame = con.execute(f"""
        SELECT eodhd_code AS code, min(settlement_date) AS si_first,
               max(settlement_date) AS si_last
        FROM finra_short_interest
        WHERE listed AND security_kind = 'common' AND eodhd_code IS NOT NULL
          AND eodhd_code NOT IN (
            SELECT DISTINCT ticker FROM prices WHERE lane IN ({lanes}))
        GROUP BY 1 ORDER BY 1
        """).df()
    latest = con.execute("SELECT max(settlement_date) FROM finra_short_interest").fetchone()[0]
    for column in ("si_first", "si_last"):
        frame[column] = pd.to_datetime(frame[column]).dt.date
    return frame, pd.Timestamp(latest).date()


def fetch_symbol_list(session: requests.Session, *, delisted: bool) -> dict[str, dict]:
    data = _api_get(
        session,
        f"exchange-symbol-list/{EODHD_US_EXCHANGE}",
        {"delisted": "1"} if delisted else None,
    )
    if not data or not isinstance(data, list):
        raise RuntimeError(
            f"empty {'delisted' if delisted else 'active'} US symbol list from the provider"
        )
    return {str(row.get("Code", "")).strip(): row for row in data if row.get("Code")}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--plan", action="store_true", help="print the counts and write nothing"
    )
    args = parser.parse_args()

    import explore_eodhd

    wanted, latest = wanted_symbols(explore_eodhd.connect())
    log.info("Short interest symbols without prices: %d (latest report %s)", len(wanted), latest)

    session = requests.Session()
    session.params = {"api_token": _get_api_key()}
    active = fetch_symbol_list(session, delisted=False)
    delisted = fetch_symbol_list(session, delisted=True)
    log.info("Provider lists: %d active, %d delisted", len(active), len(delisted))

    universe, unmatched = match_symbols(wanted, active, delisted, latest=latest)
    kept = universe["Type"].astype(str).str.lower().isin(KEEP_TYPES)
    log.info(
        "Dropped %d non-equity matches: %s",
        int((~kept).sum()),
        universe.loc[~kept, "Type"].value_counts().to_dict(),
    )
    universe = universe[kept].reset_index(drop=True)
    log.info(
        "Matched %d (%d delisted, %d active); unmatched %d",
        len(universe),
        int(universe["delisted"].sum()),
        int((~universe["delisted"]).sum()),
        len(unmatched),
    )
    log.info("By type: %s", universe["Type"].value_counts().head(8).to_dict())
    log.info(
        "First fill costs about %d price calls and %d split calls", len(universe), len(universe)
    )
    if args.plan:
        log.info("--plan: nothing written")
        return
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(universe, TICKERS_PATH, index=False)
    log.info("Wrote %s", TICKERS_PATH)
    leftovers = wanted[wanted["code"].isin(unmatched)].sort_values("code")
    _atomic.to_csv(leftovers, UNMATCHED_PATH, index=False)
    log.info("Wrote %s (%d symbols)", UNMATCHED_PATH, len(leftovers))


if __name__ == "__main__":
    main()
