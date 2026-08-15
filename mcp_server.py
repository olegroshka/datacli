"""MCP server exposing datacli's read-only data tools to MCP clients.

Lets Claude Code / Claude Desktop / Cursor query your local snapshot directly:
guarded read-only SQL over the DuckDB views (prices, dividends, splits,
fundamentals, news and their *_state sidecars; catalog once reindexed; macro,
macro_country, macro_market once fetched), the schema, and the lane registry.
EODHD views carry a ``lane`` column; the macro views join by date.

Run it (after ``uv sync --extra mcp``):

    uv run python mcp_server.py            # stdio transport

Register with Claude Code:

    claude mcp add datacli -- uv run --extra mcp python mcp_server.py

The tool *logic* below is plain, testable functions; the MCP wiring in
``build_server`` imports the ``mcp`` package lazily, so this module is importable
(and unit-testable) without the extra installed.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parent
for _p in (_REPO, _REPO / "eodhd"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from lab import sqlguard  # noqa: E402

_CON: Any = None


def _connection() -> Any:
    """Lazily build (once) the read-only DuckDB connection over eodhd + macro."""
    global _CON
    if _CON is None:
        from lab import data as lab_data

        _CON = lab_data.connect()
    return _CON


# --------------------------------------------------------------------------- #
# tool logic (plain, testable)
# --------------------------------------------------------------------------- #
def run_sql(query: str, *, con: Any = None, max_rows: int = 200) -> dict[str, Any]:
    """Run a read-only ``SELECT``/``WITH`` query; return columns + rows or an error."""
    ok, err, normalized = sqlguard.validate(query, max_rows=max_rows)
    if not ok:
        return {"error": err}
    connection = con if con is not None else _connection()
    try:
        cur = connection.execute(normalized)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchmany(max_rows)]
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {"columns": columns, "rows": rows, "sql": normalized}


def schema(con: Any = None) -> str:
    """A description of the queryable views and their columns."""
    from lab.data import schema_text

    return schema_text(con if con is not None else _connection())


def lanes() -> list[dict[str, Any]]:
    """The data lanes and their datasets."""
    from eodhd_datasets import LANES  # type: ignore[import-not-found]

    return [
        {
            "lane": lane.name,
            "region": lane.region,
            "asset_class": lane.asset_class,
            "datasets": [ds.display for ds in lane.datasets],
        }
        for lane in LANES.values()
    ]


# --------------------------------------------------------------------------- #
# MCP wiring (imports `mcp` lazily)
# --------------------------------------------------------------------------- #
def build_server() -> Any:
    from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

    server = FastMCP("datacli")

    @server.tool()
    def sql(query: str) -> dict:
        """Run a read-only SELECT/WITH query over datacli's DuckDB views (prices,
        dividends, splits, fundamentals, news, and macro / macro_country / catalog
        when present). EODHD views have a `lane` column; macro views join by
        date. Returns {columns, rows, sql}."""
        return run_sql(query)

    @server.tool()
    def describe_schema() -> str:
        """List the queryable views and their canonical columns."""
        return schema()

    @server.tool()
    def list_lanes() -> list:
        """List the data lanes (region, asset class) and their datasets."""
        return lanes()

    return server


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
