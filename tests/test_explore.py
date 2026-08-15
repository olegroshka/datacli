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
    # mirror a real projected view: canonical columns present (NULL-filled) + lane
    con.execute(
        "CREATE VIEW dividends AS SELECT * FROM (VALUES "
        "('AAA','US','2026-06-30',0.5,0.5,'USD','Q','2026-07-05','us_common'),"
        "('AAA','US','2026-03-31',0.4,0.4,'USD','Q','2026-04-05','us_common')) "
        "t(ticker,exchange,ex_date,dividend,unadjusted_dividend,"
        "currency,period,payment_date,lane)"
    )
    con.execute(
        "CREATE VIEW dividends_state AS SELECT * FROM (VALUES "
        "('AAA','US','2026-07-08','2026-06-30','ok','us_common')) "
        "t(ticker,exchange,coverage_through,latest_data_date,status,lane)"
    )
    # each verb runs cleanly against the synthetic views
    assert ex.find(con, "AA") == 0
    assert ex.rows(con, "AAA.US", "dividends", 10, None) == 0
    # explicit column projection also works
    assert ex.rows(con, "AAA.US", "dividends", 10, "ex_date,dividend") == 0
    assert ex.describe(con, "AAA.US") == 0
    assert ex.coverage(con, "AAA.US") == 0
    assert ex.run_sql(con, "SELECT count(*) FROM dividends", 50) == 0
    # a ticker absent from every view still returns cleanly
    assert ex.describe(con, "ZZZ.US") == 0


def test_run_sql_missing_view_is_friendly(capsys) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()  # a fresh root: no dataset views at all
    assert ex.run_sql(con, "SELECT * FROM prices", 50) == 1
    out = capsys.readouterr().out
    assert "no dataset views on this connection" in out
    assert "First fill" in out
    assert "Traceback" not in out
    # with some views registered, the message lists what *does* exist
    con.execute("CREATE VIEW dividends AS SELECT 1 AS x")
    con.execute("CREATE VIEW dividends_state AS SELECT 1 AS x")
    assert ex.run_sql(con, "SELECT * FROM prices", 50) == 1
    out = capsys.readouterr().out
    assert "views on this connection" in out
    assert "dividends" in out and "dividends_state" in out
    assert "First fill" not in out  # data exists; the hint is for empty roots only
    # other errors keep the plain exception path
    assert ex.run_sql(con, "SELEC 1", 50) == 1
    out = capsys.readouterr().out
    assert "First fill" not in out


def test_build_parser_prog_and_verb_help(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("DATACLI_PROG", raising=False)
    assert ex.build_parser().prog == "explore"
    # cli.py may hand over the bare launcher or launcher+verb; both read well
    assert ex._sub_prog("eodhd", "rows") == "eodhd rows"
    assert ex._sub_prog("eodhd rows", "rows") == "eodhd rows"
    monkeypatch.setenv("DATACLI_PROG", "eodhd rows")
    with pytest.raises(SystemExit) as exc:
        ex.build_parser().parse_args(["rows", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: eodhd rows ")
    assert "latest N rows" in out
    assert "ticker-keyed" in out
    monkeypatch.setenv("DATACLI_PROG", "eodhd")
    with pytest.raises(SystemExit):
        ex.build_parser().parse_args(["reindex", "--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: eodhd reindex")
    assert "run this after every fetch" in out
