"""Register read-only FINRA views on a DuckDB connection.

Layers onto an existing connection (the eodhd explorer's, the lab's, the MCP
server's):

- ``finra_short_volume`` -- one row per ``(date, symbol)``: the stored
  columns plus ``security_kind``, ``eodhd_code`` (the EODHD ``Code`` for a
  common symbol, ``BRK/B`` -> ``BRK-B``, NULL otherwise) and ``short_ratio``
  (``short_volume / total_volume``). Join to ``prices`` on
  ``eodhd_code = ticker AND date = date`` (``us_common`` lane).
- ``finra_short_volume_state`` -- the fetch-state sidecar, one row per
  attempted trade date (``ok`` / ``absent`` / ``error``).
- ``finra_weekly_flow`` -- every weekly ATS / OTC flow row as published, plus
  ``eodhd_code``; ``finra_weekly_flow_symbol`` -- the per-symbol totals
  pivoted to ATS and OTC shares, trades and notional, one row per
  ``(week_start, tier, symbol)``, with ``published_at`` for point-in-time
  joins (ASOF on ``published_at``, never on ``week_start``).
- ``finra_weekly_flow_state`` -- that dataset's sidecar.

The symbol mapping is the SQL twin of :func:`finra.short_volume.security_kind`
and :func:`finra.short_volume.eodhd_code`; a test keeps the two in step.
Best-effort: a view appears only once its data exists. Idempotent
(``CREATE OR REPLACE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finra import short_volume as sv
from finra import weekly_flow as wf
from finra.config import finra_root

VIEW = "finra_short_volume"
STATE_VIEW = "finra_short_volume_state"
FLOW_VIEW = "finra_weekly_flow"
FLOW_SYMBOL_VIEW = "finra_weekly_flow_symbol"
FLOW_STATE_VIEW = "finra_weekly_flow_state"

# the SQL twin of short_volume.security_kind / eodhd_code
_KIND_SQL = """CASE
        WHEN symbol LIKE '%/WS' THEN 'warrant'
        WHEN symbol LIKE '%/U' THEN 'unit'
        WHEN symbol LIKE '%/R' THEN 'right'
        WHEN regexp_matches(symbol, '^[A-Z]+p[A-Z]?$') THEN 'preferred'
        WHEN regexp_matches(symbol, '^[A-Z]+(/[A-Z])?$') THEN 'common'
        ELSE 'other'
    END"""
_CODE_SQL = "CASE WHEN security_kind = 'common' THEN replace(symbol, '/', '-') END"

# The weekly flow data uses the listing market's spelling instead of the SIP's:
# a share class is ``BRK.B`` / ``BF.A`` (dot), not ``BRK/B``. Same mapping, both
# separators accepted. The SQL twin of weekly_flow.eodhd_code.
_FLOW_KIND_SQL = """CASE
        WHEN symbol LIKE '%/WS' OR symbol LIKE '%.WS' THEN 'warrant'
        WHEN symbol LIKE '%/U' OR symbol LIKE '%.U' THEN 'unit'
        WHEN symbol LIKE '%/R' OR symbol LIKE '%.R' THEN 'right'
        WHEN regexp_matches(symbol, '^[A-Z]+p[A-Z]?$') THEN 'preferred'
        WHEN regexp_matches(symbol, '^[A-Z]+([./][A-Z])?$') THEN 'common'
        ELSE 'other'
    END"""
_FLOW_CODE_SQL = (
    "CASE WHEN security_kind = 'common' "
    "THEN replace(replace(symbol, '/', '-'), '.', '-') END"
)


def _state_view(con: Any, name: str, path: Path) -> None:
    if path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS "
            f"SELECT * FROM read_csv_auto('{path.as_posix()}', all_varchar=true)"
        )


def _register_short_volume(con: Any, root: Path) -> bool:
    store = sv.store(root)
    if not store.days_on_disk():
        return False
    glob = (store.daily_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {VIEW} AS "
        "SELECT date, symbol, short_volume, short_exempt_volume, total_volume, "
        "facilities, source, security_kind, "
        f"{_CODE_SQL} AS eodhd_code, "
        "CASE WHEN total_volume > 0 THEN short_volume / total_volume END AS short_ratio, "
        "total_volume - short_volume AS long_volume, "
        "CASE WHEN total_volume > 0 THEN 1 - short_volume / total_volume END AS long_ratio "
        f"FROM (SELECT *, {_KIND_SQL} AS security_kind FROM read_parquet('{glob}'))"
    )
    _state_view(con, STATE_VIEW, store.state_path)
    return True


def _register_weekly_flow(con: Any, root: Path) -> bool:
    store = wf.store(root)
    if not store.partitions_on_disk():
        return False
    glob = (store.daily_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {FLOW_VIEW} AS "
        f"SELECT *, {_FLOW_CODE_SQL} AS eodhd_code "
        f"FROM (SELECT *, {_FLOW_KIND_SQL} AS security_kind FROM read_parquet('{glob}'))"
    )
    con.execute(
        f"CREATE OR REPLACE VIEW {FLOW_SYMBOL_VIEW} AS "
        "SELECT week_start, tier, symbol, any_value(security_kind) AS security_kind, "
        "any_value(eodhd_code) AS eodhd_code, "
        "sum(CASE WHEN summary_type = 'ATS_W_SMBL' THEN share_quantity END) AS ats_shares, "
        "sum(CASE WHEN summary_type = 'ATS_W_SMBL' THEN trade_count END) AS ats_trades, "
        "sum(CASE WHEN summary_type = 'ATS_W_SMBL' THEN notional END) AS ats_notional, "
        "sum(CASE WHEN summary_type = 'OTC_W_SMBL' THEN share_quantity END) AS otc_shares, "
        "sum(CASE WHEN summary_type = 'OTC_W_SMBL' THEN trade_count END) AS otc_trades, "
        "sum(CASE WHEN summary_type = 'OTC_W_SMBL' THEN notional END) AS otc_notional, "
        "max(published_at) AS published_at, max(updated_at) AS updated_at "
        f"FROM {FLOW_VIEW} WHERE summary_type IN ('ATS_W_SMBL', 'OTC_W_SMBL') AND symbol IS NOT NULL "
        "GROUP BY week_start, tier, symbol"
    )
    _state_view(con, FLOW_STATE_VIEW, store.state_path)
    return True


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    """Register whichever FINRA surfaces have data. Returns ``{view: registered}``."""
    base = Path(root) if root is not None else finra_root()
    return {
        VIEW: _register_short_volume(con, base),
        FLOW_VIEW: _register_weekly_flow(con, base),
    }


def schema_snippet(*, short_volume: bool = True, weekly_flow: bool = True) -> str:
    parts = ["FINRA views (join to equities by eodhd_code = ticker; dates as noted):"]
    if short_volume:
        parts += [
            f"- {VIEW}(date, symbol, short_volume, short_exempt_volume, total_volume, "
            "facilities, source, security_kind, eodhd_code, short_ratio, long_volume, long_ratio)",
            "  [Reg SHO daily short sale volume, consolidated NMS, one row per symbol per trade date;",
            "  join on date; FINRA publishes day T at 18:00 ET, after the close of T]",
            "  symbol is FINRA's SIP spelling (BRK/B, ABRpD, AACT/WS); eodhd_code maps common",
            "  symbols to EODHD tickers (BRK/B -> BRK-B), NULL for preferred / warrant / unit / right;",
            "  short_ratio = short_volume / total_volume; volumes are shares (fractional since 2026)",
            f"- {STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    if weekly_flow:
        parts += [
            f"- {FLOW_SYMBOL_VIEW}(week_start, tier, symbol, security_kind, eodhd_code, ats_shares, "
            "ats_trades, ats_notional, otc_shares, otc_trades, otc_notional, published_at, updated_at)",
            "  [weekly ATS (dark pool) and OTC market-maker flow per NMS symbol, tiers T1/T2;",
            "  point-in-time: FINRA publishes T1 three weeks and T2 five weeks after the week's",
            "  Monday, so ASOF-join on published_at, never on week_start]",
            f"- {FLOW_VIEW}(week_start, tier, summary_type, symbol, issue_name, mpid, firm_crd, "
            "participant_name, product_type, trade_count, share_quantity, notional, published_at, "
            "updated_at, last_reported_at, source, security_kind, eodhd_code)",
            "  [every published row; summary_type ATS_W_SMBL_FIRM / OTC_W_SMBL_FIRM are per symbol per",
            "  venue (mpid), ATS_W_SMBL / OTC_W_SMBL per symbol, ATS_W_FIRM / OTC_W_FIRM per venue]",
            f"- {FLOW_STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    return "\n".join(parts)
