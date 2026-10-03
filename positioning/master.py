"""A dated CUSIP to ticker map as a view (INV-002 R10, WP4, first cut).

13F holdings are keyed by CUSIP, prices and short interest by ticker. The
SEC's fails-to-deliver files carry both on every settlement date, so the pairs
seen there, with the dates they were seen, are a map that follows ticker
changes and reuse through time. Coverage is "every security that failed to
deliver at least once", which over the years is nearly every traded name.

``positioning_cusip_map`` has one row per ``(cusip, eodhd_code)`` with
``first_seen``, ``last_seen`` and ``n_days``. To resolve a 13F row for period
``p``, pick the row with ``first_seen <= p <= last_seen`` (or the one with
``last_seen`` closest to ``p``); a CUSIP with two tickers over time is a
rename, a ticker with two CUSIPs a reuse.
"""

from __future__ import annotations

from typing import Any

VIEW = "positioning_cusip_map"
FTD_VIEW = "finra_fails_to_deliver"


def _has(con: Any, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name = ?", [name]
        ).fetchone()
    )


def register(con: Any) -> bool:
    if not _has(con, FTD_VIEW):
        return False
    con.execute(
        f"CREATE OR REPLACE VIEW {VIEW} AS "
        "SELECT cusip, eodhd_code, any_value(symbol) AS symbol, "
        "min(settlement_date) AS first_seen, max(settlement_date) AS last_seen, "
        "count(DISTINCT settlement_date) AS n_days, "
        "any_value(description) AS description "
        f"FROM {FTD_VIEW} "
        "WHERE cusip IS NOT NULL AND cusip <> '' AND eodhd_code IS NOT NULL AND eodhd_code <> '' "
        "GROUP BY cusip, eodhd_code"
    )
    return True


def schema_snippet() -> str:
    return "\n".join(
        [
            f"- {VIEW}(cusip, eodhd_code, symbol, first_seen, last_seen, n_days, description)",
            "  [dated CUSIP <-> ticker pairs from the SEC fails-to-deliver files, 2018 on; the",
            "  bridge from sec_13f_holdings.cusip to prices.ticker; a CUSIP with several tickers",
            "  over time is a rename, pick the row whose first_seen..last_seen covers the date]",
        ]
    )
