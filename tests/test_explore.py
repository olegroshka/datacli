from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import explore_eodhd as ex  # type: ignore  # noqa: E402


def test_parse_ticker() -> None:
    assert ex.parse_ticker("VAR.OL") == ("VAR", "OL")
    assert ex.parse_ticker("var") == ("VAR", None)
    assert ex.parse_ticker("  aapl.us ") == ("AAPL", "US")
    # rsplit on the last dot
    assert ex.parse_ticker("BRK.B") == ("BRK", "B")


def test_where_clause() -> None:
    assert ex._where("VAR", "OL") == (
        "upper(ticker) = ? AND upper(exchange) = ?",
        ["VAR", "OL"],
    )
    assert ex._where("VAR", None) == ("upper(ticker) = ?", ["VAR"])


def test_verbs_on_synthetic_views() -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute(
        "CREATE VIEW dividends AS SELECT * FROM (VALUES "
        "('AAA','US','2026-06-30',0.5,'us_common'),"
        "('AAA','US','2026-03-31',0.4,'us_common')) "
        "t(ticker,exchange,ex_date,dividend,lane)"
    )
    con.execute(
        "CREATE VIEW dividends_state AS SELECT * FROM (VALUES "
        "('AAA','US','2026-07-08','2026-06-30','ok','us_common')) "
        "t(ticker,exchange,coverage_through,latest_data_date,status,lane)"
    )
    # each verb runs cleanly against the synthetic views
    assert ex.find(con, "AA") == 0
    assert ex.rows(con, "AAA.US", "dividends", 10) == 0
    assert ex.describe(con, "AAA.US") == 0
    assert ex.coverage(con, "AAA.US") == 0
    assert ex.run_sql(con, "SELECT count(*) FROM dividends", 50) == 0
    # a ticker absent from every view still returns cleanly
    assert ex.describe(con, "ZZZ.US") == 0
