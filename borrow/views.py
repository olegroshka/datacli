"""Register the read-only borrow views on a DuckDB connection.

- ``borrow_us`` -- one row per ``(snapshot_date, symbol, currency)``: Interactive
  Brokers' shortable list as of that day (``fee_rate`` and ``rebate_rate`` in
  percent a year, ``available`` shares), with ``eodhd_code`` by the usual
  spelling rule for common symbols. ``snapshot_date`` is the file's own date
  (New York); the snapshot is taken after the US close, so a reader on day
  ``d`` may use rows with ``snapshot_date < d``.
- ``borrow_us_state`` -- the fetch-state sidecar.

Best-effort: the view appears only once a snapshot exists. Idempotent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from borrow import ib
from borrow.config import borrow_root

VIEW = "borrow_us"
STATE_VIEW = "borrow_us_state"


def register(con: Any, *, root: Path | None = None) -> dict[str, bool]:
    base = Path(root) if root is not None else borrow_root()
    target = ib.store(base)
    if not target.days_on_disk():
        return {VIEW: False}
    glob = (target.daily_dir / "*.parquet").as_posix()
    con.execute(
        f"CREATE OR REPLACE VIEW {VIEW} AS "
        "SELECT *, CASE WHEN currency = 'USD' AND regexp_matches(symbol, '^[A-Z]+( [A-Z])?$') "
        "THEN replace(symbol, ' ', '-') END AS eodhd_code "
        f"FROM read_parquet('{glob}')"
    )
    if target.state_path.exists():
        con.execute(
            f"CREATE OR REPLACE VIEW {STATE_VIEW} AS SELECT * FROM "
            f"read_csv_auto('{target.state_path.as_posix()}', all_varchar=true)"
        )
    return {VIEW: True}


def schema_snippet() -> str:
    return "\n".join(
        [
            "Borrow views (Interactive Brokers' shortable list, one snapshot a day):",
            f"- {VIEW}(snapshot_date, symbol, currency, name, con_id, isin, rebate_rate, fee_rate, "
            "available, available_capped, figi, eodhd_code)",
            "  [fee_rate and rebate_rate in percent a year (NULL where the broker says NA), available =",
            "  shares the broker can lend (available_capped: the broker reports '>10000000');",
            "  eodhd_code for USD common symbols (a space in the symbol is",
            "  a share class, BRK B -> BRK-B); taken after the US close, so use snapshot_date < d]",
            f"- {STATE_VIEW}(date, status, source, rows, short_sum, total_sum, sha256, fetched_at, detail, part)",
        ]
    )
