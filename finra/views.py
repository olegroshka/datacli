"""Register read-only FINRA views on a DuckDB connection.

Layers onto an existing connection (the eodhd explorer's, the lab's, the MCP
server's):

- ``finra_short_volume`` -- one row per ``(date, symbol)``: the stored
  columns plus ``security_kind``, ``eodhd_code`` (the EODHD ``Code`` for a
  common symbol, ``BRK/B`` -> ``BRK-B``, NULL otherwise), ``short_ratio``
  (``short_volume / total_volume``), ``long_volume`` and ``long_ratio``.
  Join to ``prices`` on ``eodhd_code = ticker AND date = date``.
- ``finra_weekly_flow`` -- every weekly ATS / OTC flow row as published, plus
  ``eodhd_code``; ``finra_weekly_flow_symbol`` -- the per-symbol totals
  pivoted to ATS and OTC shares, trades and notional, with ``published_at``
  for point-in-time joins (ASOF on ``published_at``, never on ``week_start``).
- ``finra_short_interest`` -- one row per ``(settlement_date, symbol)`` as
  published, plus ``listed``, ``security_kind`` (from the issue name, since
  these symbols carry no separators) and ``eodhd_code`` (class shares such as
  ``BRKB`` are matched to the SIP spelling ``BRK/B`` in the short volume store
  and mapped to ``BRK-B``). ``published_at`` is the point-in-time column.
  ``finra_short_interest_float`` adds ``short_over_outstanding`` (short
  position over the EODHD quarterly shares outstanding, taken as known 45
  days after the quarter end) and ``short_over_float_now`` (over the current
  EODHD float snapshot; NOT point-in-time, for a quick look only).
- ``finra_*_state`` -- each dataset's fetch-state sidecar.

The symbol mappings are the SQL twins of the providers' Python rules; tests
keep them in step. Best-effort: a view appears only once its data exists.
Idempotent (``CREATE OR REPLACE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from finra import fails_to_deliver as ftd
from finra import short_interest as si
from finra import short_volume as sv
from finra import weekly_flow as wf
from finra.config import finra_root

VIEW = "finra_short_volume"
STATE_VIEW = "finra_short_volume_state"
FLOW_VIEW = "finra_weekly_flow"
FLOW_SYMBOL_VIEW = "finra_weekly_flow_symbol"
FLOW_STATE_VIEW = "finra_weekly_flow_state"
SI_VIEW = "finra_short_interest"
SI_FLOAT_VIEW = "finra_short_interest_float"
SI_STATE_VIEW = "finra_short_interest_state"
FTD_VIEW = "finra_fails_to_deliver"
FTD_STATE_VIEW = "finra_fails_to_deliver_state"
#: Days after a quarter end by which its share count is taken as public (10-Q due in 40).
SHARES_AVAILABLE_LAG_DAYS = 45

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

# Short interest symbols carry no separators, so the kind comes from the issue
# name and the market class; class shares are resolved through the SIP lookup.
_SI_KIND_SQL = """CASE
        WHEN NOT listed THEN 'otc'
        WHEN issue_name ILIKE '%preferred%' OR issue_name ILIKE '%pfd%'
             OR issue_name ILIKE '% pref%' THEN 'preferred'
        WHEN issue_name ILIKE '%warrant%' THEN 'warrant'
        WHEN issue_name ILIKE '% unit%' OR issue_name ILIKE '%units%' THEN 'unit'
        WHEN issue_name ILIKE '% right%' THEN 'right'
        ELSE 'common'
    END"""


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


def _class_share_lookup_sql(root: Path) -> str:
    """``(flat, eodhd_code)`` for every class share the short volume store knows:
    ``BRKB`` -> ``BRK-B``. Empty when there is no short volume data."""
    store = sv.store(root)
    if not store.days_on_disk():
        return "SELECT NULL::VARCHAR AS flat, NULL::VARCHAR AS eodhd_code WHERE false"
    glob = (store.daily_dir / "*.parquet").as_posix()
    return (
        "SELECT DISTINCT replace(symbol, '/', '') AS flat, replace(symbol, '/', '-') AS eodhd_code "
        f"FROM read_parquet('{glob}') WHERE regexp_matches(symbol, '^[A-Z]+/[A-Z]$')"
    )


def _eodhd_us_common_dir() -> Path | None:
    try:
        import config as eodhd_config  # type: ignore[import-not-found]

        root, _ = eodhd_config.eodhd_data_root()
        return Path(root) / "us_common"
    except Exception:
        return None


def _register_short_interest(con: Any, root: Path) -> bool:
    store = si.store(root)
    if not store.days_on_disk():
        return False
    glob = (store.daily_dir / "*.parquet").as_posix()
    listed = ", ".join(f"'{c}'" for c in si.LISTED_CLASSES)
    con.execute(
        f"CREATE OR REPLACE VIEW {SI_VIEW} AS "
        "WITH lookup AS (" + _class_share_lookup_sql(root) + "), "
        f"base AS (SELECT *, market_class IN ({listed}) AS listed FROM read_parquet('{glob}')), "
        f"kinds AS (SELECT *, {_SI_KIND_SQL} AS security_kind FROM base) "
        "SELECT k.*, CASE WHEN k.security_kind = 'common' THEN coalesce(l.eodhd_code, k.symbol) END AS eodhd_code "
        "FROM kinds k LEFT JOIN lookup l ON l.flat = k.symbol"
    )
    _state_view(con, SI_STATE_VIEW, store.state_path)
    us_common = _eodhd_us_common_dir()
    quarterly = (
        us_common / "outstanding_shares_quarterly.parquet" if us_common else None
    )
    snapshot = us_common / "shares_stats_snapshot.parquet" if us_common else None
    if quarterly is not None and quarterly.exists():
        snap_sql = (
            f"SELECT ticker, shares_float FROM read_parquet('{snapshot.as_posix()}')"
            if snapshot is not None and snapshot.exists()
            else "SELECT NULL::VARCHAR AS ticker, NULL::BIGINT AS shares_float WHERE false"
        )
        con.execute(
            f"CREATE OR REPLACE VIEW {SI_FLOAT_VIEW} AS "
            "WITH shares AS ("
            "  SELECT upper(ticker) AS ticker, CAST(date_formatted AS DATE) AS quarter_end, "
            f"         CAST(date_formatted AS DATE) + INTERVAL {SHARES_AVAILABLE_LAG_DAYS} DAY AS available_at, "
            "         shares AS shares_outstanding "
            f"  FROM read_parquet('{quarterly.as_posix()}') WHERE shares IS NOT NULL AND shares > 0"
            "), snap AS (" + snap_sql + ") "
            "SELECT s.settlement_date, s.published_at, s.symbol, s.eodhd_code, s.short_position, "
            "s.days_to_cover, sh.quarter_end, sh.shares_outstanding, "
            "s.short_position / sh.shares_outstanding AS short_over_outstanding, "
            "CASE WHEN snap.shares_float > 0 THEN s.short_position / snap.shares_float END AS short_over_float_now "
            f"FROM {SI_VIEW} s "
            "ASOF LEFT JOIN shares sh ON sh.ticker = s.eodhd_code AND sh.available_at <= CAST(s.settlement_date AS TIMESTAMP) "
            "LEFT JOIN snap ON snap.ticker = s.eodhd_code "
            "WHERE s.eodhd_code IS NOT NULL"
        )
    return True


def _register_fails(con: Any, root: Path) -> bool:
    store = ftd.store(root)
    if not store.days_on_disk():
        return False
    glob = (store.daily_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {FTD_VIEW} AS "
        "WITH lookup AS (" + _class_share_lookup_sql(root) + ") "
        "SELECT f.*, coalesce(l.eodhd_code, f.symbol) AS eodhd_code "
        f"FROM read_parquet('{glob}') f LEFT JOIN lookup l ON l.flat = f.symbol"
    )
    _state_view(con, FTD_STATE_VIEW, store.state_path)
    return True


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    """Register whichever FINRA surfaces have data. Returns ``{view: registered}``."""
    base = Path(root) if root is not None else finra_root()
    return {
        VIEW: _register_short_volume(con, base),
        FLOW_VIEW: _register_weekly_flow(con, base),
        SI_VIEW: _register_short_interest(con, base),
        FTD_VIEW: _register_fails(con, base),
    }


def schema_snippet(
    *,
    short_volume: bool = True,
    weekly_flow: bool = True,
    short_interest: bool = True,
    fails_to_deliver: bool = True,
) -> str:
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
    if short_interest:
        parts += [
            f"- {SI_VIEW}(settlement_date, symbol, issue_name, market_class, exchange_code, "
            "short_position, previous_short_position, change_shares, change_percent, "
            "average_daily_volume, days_to_cover, revised, split_adjusted, published_at, source, "
            "listed, security_kind, eodhd_code)",
            "  [consolidated short interest twice a month at mid-month and month-end settlement",
            "  dates, every market class (listed = NYSE/NNM/ARCA/BZX/AMEX/SC, else OTC);",
            "  point-in-time: published_at = settlement + 7 business days, ASOF-join on it]",
            f"- {SI_FLOAT_VIEW}(settlement_date, published_at, symbol, eodhd_code, short_position, "
            "days_to_cover, quarter_end, shares_outstanding, short_over_outstanding, short_over_float_now)",
            "  [short position over EODHD quarterly shares outstanding known 45 days after the quarter",
            "  end (point-in-time), and over today's float snapshot (NOT point-in-time)]",
            f"- {SI_STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    if fails_to_deliver:
        parts += [
            f"- {FTD_VIEW}(settlement_date, cusip, symbol, quantity, description, price, half_start, "
            "published_at, source, eodhd_code)",
            "  [SEC CNS fails to deliver per settlement date and CUSIP, every listed and OTC security;",
            "  point-in-time: the SEC posts each half-month file two to four weeks later, published_at",
            "  is that rule plus a 5-day margin, ASOF-join on it; symbols are separator-free (BRKB)",
            "  and eodhd_code resolves class shares through the short volume store]",
            f"- {FTD_STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    return "\n".join(parts)
