from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import mcp_server  # type: ignore  # noqa: E402


def _con():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE VIEW t AS SELECT * FROM (VALUES (1), (2)) v(id)")
    return con


def test_run_sql_returns_rows() -> None:
    result = mcp_server.run_sql("SELECT id FROM t ORDER BY id", con=_con())
    assert result["columns"] == ["id"]
    assert result["rows"] == [[1], [2]]
    assert "sql" in result


def test_run_sql_rejects_writes() -> None:
    result = mcp_server.run_sql("DROP VIEW t", con=_con())
    assert "error" in result and "rows" not in result


def test_run_sql_surfaces_db_errors() -> None:
    result = mcp_server.run_sql("SELECT nope FROM t", con=_con())
    assert "error" in result


def test_lanes_lists_registry() -> None:
    out = mcp_server.lanes()
    names = {lane["lane"] for lane in out}
    assert {"us_common", "uk_eu"} <= names
    assert all("datasets" in lane for lane in out)
