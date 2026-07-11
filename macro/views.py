"""Register read-only ``macro`` views on a DuckDB connection.

Layers three views onto an existing connection (e.g. the eodhd explorer's), so the
lab agent can query macro series and join them to the equity data by date:
``macro_obs`` (raw), ``macro_series`` (metadata from the registry), and the joined
``macro`` view. Idempotent (``CREATE OR REPLACE``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from macro import registry as reg
from macro.config import OBSERVATIONS, observations_path


def register(con: Any, *, root: Path | None = None, series: dict | None = None) -> bool:
    """Build the macro views if the observations parquet exists. Returns success."""
    catalog = series if series is not None else reg.SERIES
    path = (Path(root) / OBSERVATIONS) if root is not None else observations_path()
    if not path.exists() or not catalog:
        return False

    con.execute(
        "CREATE OR REPLACE VIEW macro_obs AS "
        f"SELECT series_id, CAST(date AS DATE) AS date, value "
        f"FROM read_parquet('{path.as_posix()}')"
    )
    rows = ", ".join(
        "('{}', '{}', '{}')".format(sid, s.name.replace("'", "''"), s.category)
        for sid, s in catalog.items()
    )
    con.execute(
        "CREATE OR REPLACE VIEW macro_series AS "
        f"SELECT * FROM (VALUES {rows}) v(series_id, name, category)"
    )
    con.execute(
        "CREATE OR REPLACE VIEW macro AS "
        "SELECT o.series_id, s.name, s.category, o.date, o.value "
        "FROM macro_obs o LEFT JOIN macro_series s USING (series_id)"
    )
    return True


def schema_snippet(series: dict | None = None) -> str:
    catalog = series if series is not None else reg.SERIES
    examples = "; ".join(f"{sid} ({s.name})" for sid, s in list(catalog.items())[:6])
    return (
        "Macro views (FRED economic series; join to equities by date):\n"
        "- macro(series_id, name, category, date, value)\n"
        "- macro_series(series_id, name, category)\n"
        f"  categories: {', '.join(reg.categories())}\n"
        f"  example series: {examples}"
    )
