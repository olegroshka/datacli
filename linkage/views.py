"""Register the read-only linkage views on a DuckDB connection.

- ``etf_betas`` -- one row per ``(as_of, ticker, etf, window)``: trailing
  betas and correlations of daily total returns, plain and net of SPY, over
  the ``window`` sessions up to and including ``as_of`` (a month end). Use a
  snapshot from the session after ``as_of`` on.
- ``auction_abnormal`` -- one row per ``(date, ticker, side)``: the auction's
  dollar value, its trailing baseline, ``abnormal`` against it and ``resid``
  net of that day's members. Known after that day's close.
- ``auction_cospike`` -- one row per ``(as_of, ticker, neighbor)``: the
  ``top_k`` neighbours by correlation of auction residuals over the trailing
  year, with joint spike counts.

Best-effort and idempotent: a view appears only once its files exist.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from linkage.config import linkage_root
from linkage.datasets import NAMES


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    base = Path(root) if root is not None else linkage_root()
    made: dict[str, bool] = {}
    for name in NAMES:
        directory = base / name
        if not (directory.is_dir() and any(directory.glob("*.parquet"))):
            made[name] = False
            continue
        con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM read_parquet('{(directory / '*.parquet').as_posix()}')"
        )
        made[name] = True
    return made


def schema_snippet() -> str:
    return "\n".join(
        [
            "Linkage views (point in time by construction; behaviour, not holdings):",
            "- etf_betas(as_of, ticker, etf, etf_group, window, n_obs, beta, corr, beta_ex_mkt, corr_ex_mkt)",
            "  [month-end snapshots of each US stock's trailing betas to the 38 context ETFs over 63 / 252",
            "  sessions ending at as_of; *_ex_mkt nets SPY out of both legs (NULL for the SPY row);",
            "  usable from the session after as_of]",
            "- auction_abnormal(date, ticker, side, asset_type, is_member, value_usd, log_value, baseline,",
            "  abnormal, market_abnormal, resid)",
            "  [side close|open; abnormal = log auction value minus the median of the previous 63 auction days;",
            "  resid = abnormal minus that day's median over members of the same asset type]",
            "- auction_cospike(as_of, ticker, neighbor, rank, corr, n_overlap, co_spikes, expected_co_spikes,",
            "  lift, asset_type, neighbor_asset_type)",
            "  [month-end top-20 neighbours by correlation of closing-auction resid over the trailing 252",
            "  auction days, members of the intraday lane only; usable from the session after as_of]",
        ]
    )
