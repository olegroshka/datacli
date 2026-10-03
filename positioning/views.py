"""Register read-only positioning views on a DuckDB connection.

- ``positioning_short_ladder`` -- one row per ``(eodhd_code, settlement_date)``:
  the FIFO lot ladder over FINRA short interest (DD-001). ``published_at`` is
  the point-in-time column: ASOF-join on it with a strict inequality, never on
  ``settlement_date``. ``short_profit_log`` is ``log(cost_basis / mark)``, the
  symmetric form of ``profit_pct``.
- ``positioning_long_ladder`` -- one row per ``(cusip, period, aggregate)``:
  the same ladder, ``side = LONG``, over the 13F filers' aggregate shares
  (``aggregate = 'all'``) and the hedge-fund cohort's (``'cohort'``), quarter
  by quarter (DD-002 WP9). ``flow`` is the trading of managers present in both
  quarters, ``drift_shares`` what entered or left with a manager. ASOF-join on
  ``published_at``. ``long_profit_log`` is ``log(mark / cost_basis)``.
- ``positioning_holdings_inputs`` -- one row per ``(cusip, period, aggregate)``:
  SPEC's G1 members from the same 13F panel (DD-002 WP10): ``long_fund_weight``,
  ``long_conc``, ``best_ideas`` (L1-normalised per period), ``n_holders``.
- ``positioning_*_state`` -- the build-state sidecars.
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
LONG_VIEW = "positioning_long_ladder"
LONG_STATE_VIEW = "positioning_long_ladder_state"
INPUTS_VIEW = "positioning_holdings_inputs"
INPUTS_STATE_VIEW = "positioning_holdings_inputs_state"
_HEADER = "Positioning views (derived; join to equities by eodhd_code = ticker):"


def register(
    con: Any, *, root: Path | None = None, eodhd_root: Path | None = None
) -> dict[str, bool]:
    """Register the stored ladders and the factor view; ``{view: registered}``."""
    base = Path(root) if root is not None else positioning_root()
    result = {
        VIEW: _register_ladder(
            con, base, dataset.SHORT_LADDER, VIEW, STATE_VIEW,
            "CASE WHEN profit_pct < 1 THEN -ln(1 - profit_pct) END AS short_profit_log",
        ),
        LONG_VIEW: _register_ladder(
            con, base, dataset.LONG_LADDER, LONG_VIEW, LONG_STATE_VIEW,
            "CASE WHEN profit_pct > -1 THEN ln(1 + profit_pct) END AS long_profit_log",
        ),
        INPUTS_VIEW: _register_ladder(
            con, base, dataset.HOLDINGS_INPUTS, INPUTS_VIEW, INPUTS_STATE_VIEW, "NULL AS _",
        ),
    }
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


def _register_ladder(
    con: Any, base: Path, spec: dataset.Spec, view: str, state_view: str, extra: str
) -> bool:
    target = dataset.store(base, spec)
    if not target.days_on_disk():
        return False
    glob = (target.daily_dir / "*.parquet").as_posix()
    select = "*" if extra == "NULL AS _" else f"*, {extra}"
    con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT {select} FROM read_parquet('{glob}')")
    if target.state_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {state_view} AS SELECT * FROM "
            f"read_csv_auto('{target.state_path.as_posix()}', all_varchar=true)"
        )
    return True


def schema_snippet(
    *,
    ladder: bool = True,
    factor_view: bool = True,
    cusip_map: bool = True,
    long_ladder: bool = True,
    holdings_inputs: bool = True,
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
    if long_ladder:
        lines += [
            f"- {LONG_VIEW}(cusip, period, published_at, aggregate, eodhd_code, lane, inventory, flow, "
            "drift_shares, n_lots, wavg_age_days, cost_basis, profit_pct, unrealised, realised, "
            "seed_share, gap, reset, n_managers, n_cohort_managers, level_raw, quantity_factor, "
            "price_factor, long_profit_log)",
            "  [the same ladder on the long side over SEC 13F holdings, one row per quarter end",
            "  (period) and aggregate: 'all' = every 13F filer, 'cohort' = filers whose Form ADV says",
            "  hedge funds (else private funds; cohort flags exist only for filings from 2023);",
            "  inventory = the aggregate's shares on a split-neutral basis, flow = the quarter's",
            "  trading by managers who filed both quarters, drift_shares = shares that came or went",
            "  with a manager entering or leaving the panel (not trading); profit_pct = the holders'",
            "  unrealised return on cost; filings later than 60 days after the period are left out;",
            "  point-in-time: ASOF-join on published_at (45 to 60 days after period)]",
            f"- {LONG_STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    if holdings_inputs:
        lines += [
            f"- {INPUTS_VIEW}(cusip, period, published_at, aggregate, n_holders, level_raw, value_usd, "
            "long_fund_weight, long_conc, best_ideas_count, best_ideas)",
            "  [the cross-manager distribution of each 13F position per quarter end and aggregate",
            "  ('all' / 'cohort'): long_fund_weight = sum over holders of the position's weight in the",
            "  holder's 13F book, long_conc = Herfindahl of shares across holders (1 = one holder),",
            "  best_ideas = holders for whom it is a top-10 weight, normalised to sum to 1 per period;",
            "  same filings and published_at as positioning_long_ladder; no prices needed]",
        ]
    if factor_view:
        lines.append(factors.schema_snippet())
    if cusip_map:
        lines.append(master.schema_snippet())
    return "\n".join(lines)
