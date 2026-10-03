"""Register read-only positioning views on a DuckDB connection.

- ``positioning_short_ladder`` -- one row per ``(eodhd_code, settlement_date)``:
  the FIFO lot ladder over FINRA short interest (DD-001). ``published_at`` is
  the point-in-time column: ASOF-join on it with a strict inequality, never on
  ``settlement_date``. ``short_profit_log`` is ``log(cost_basis / mark)``, the
  symmetric form of ``profit_pct``.
- ``positioning_short_ladder_state`` -- the build-state sidecar.
- ``positioning_factors`` -- style exposures and specific risk, see ``factors``.
- ``positioning_cusip_map`` -- dated CUSIP to ticker pairs, see ``master``.

Best-effort: a view appears only once its data exists. Idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from positioning import dataset, factors, master
from positioning.config import positioning_root

VIEW = "positioning_short_ladder"
STATE_VIEW = "positioning_short_ladder_state"
_HEADER = "Positioning views (derived; join to equities by eodhd_code = ticker):"


def register(
    con: Any, *, root: Path | None = None, eodhd_root: Path | None = None
) -> dict[str, bool]:
    """Register the stored ladder and the factor view; ``{view: registered}``."""
    result = {VIEW: _register_ladder(con, root)}
    try:
        if eodhd_root is None:
            import config as eodhd_config  # type: ignore[import-not-found]

            eodhd_root = Path(eodhd_config.eodhd_data_root()[0])
        result[factors.VIEW] = factors.register(con, eodhd_root=eodhd_root)
    except Exception:
        result[factors.VIEW] = False
    try:
        result[master.VIEW] = master.register(con)
    except Exception:
        result[master.VIEW] = False
    return result


def _register_ladder(con: Any, root: Path | None) -> bool:
    base = Path(root) if root is not None else positioning_root()
    target = dataset.store(base)
    if not target.days_on_disk():
        return False
    glob = (target.daily_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {VIEW} AS "
        "SELECT *, CASE WHEN profit_pct < 1 THEN -ln(1 - profit_pct) END AS short_profit_log "
        f"FROM read_parquet('{glob}')"
    )
    if target.state_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {STATE_VIEW} AS SELECT * FROM "
            f"read_csv_auto('{target.state_path.as_posix()}', all_varchar=true)"
        )
    return True


def schema_snippet(
    *, ladder: bool = True, factor_view: bool = True, cusip_map: bool = True
) -> str:
    lines = [_HEADER]
    if ladder:
        lines += [
            f"- {VIEW}(eodhd_code, settlement_date, published_at, lane, inventory, flow, n_lots, "
            "wavg_age_days, cost_basis, profit_pct, unrealised, realised, seed_share, gap, reset, "
            "quantity_factor, price_factor, short_profit_log)",
            "  [FIFO lot ladder over FINRA short interest, twice a month: inventory and flow are",
            "  short shares on a split-neutral basis (compare within a symbol, not across),",
            "  wavg_age_days = how long the open short has been held, profit_pct = short sellers'",
            "  unrealised return on cost (negative = under water); mask rows with seed_share > 0.5",
            "  (age unknown at the start of a series); lane = where its prices live (us_common,",
            "  us_extended incl. delisted names, us_etf); point-in-time: ASOF-join on published_at]",
            f"- {STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    if factor_view:
        lines.append(factors.schema_snippet())
    if cusip_map:
        lines.append(master.schema_snippet())
    return "\n".join(lines)
