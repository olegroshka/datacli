"""finra.views: the DuckDB surface, and its SQL symbol mapping kept in step with Python."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import short_volume as sv  # noqa: E402
from finra import views  # noqa: E402
from finra.store import DayState  # noqa: E402

SYMBOLS = ["A", "BRK/B", "ABRpD", "ACPpA", "AACT/WS", "AACT/U", "XYZ/R", "weird-1"]


def _seed(root: Path, day: date, symbols=SYMBOLS) -> None:
    store = sv.store(root)
    frame = pd.DataFrame(
        {
            "date": [day] * len(symbols),
            "symbol": symbols,
            "short_volume": [float(i + 1) for i in range(len(symbols))],
            "short_exempt_volume": [0.0] * len(symbols),
            "total_volume": [float(2 * (i + 1)) for i in range(len(symbols))],
            "facilities": ["B,N,Q"] * len(symbols),
            "source": ["cdn"] * len(symbols),
        }
    )
    store.write_day(day, frame)
    store.upsert_state(
        [DayState(day.isoformat(), "ok", "cdn", rows=len(symbols), sha256="s")]
    )


def test_register_noop_without_data(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    assert views.register(con, root=tmp_path) == {
        "finra_short_volume": False,
        "finra_weekly_flow": False,
    }
    assert not con.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'finra_short_volume'"
    ).fetchone()


def test_view_columns_mapping_and_ratio(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    _seed(tmp_path, date(2026, 9, 29))
    _seed(tmp_path, date(2026, 9, 30))
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)["finra_short_volume"] is True
    cols = [
        d[0]
        for d in con.execute("SELECT * FROM finra_short_volume LIMIT 0").description
    ]
    assert cols == [
        "date",
        "symbol",
        "short_volume",
        "short_exempt_volume",
        "total_volume",
        "facilities",
        "source",
        "security_kind",
        "eodhd_code",
        "short_ratio",
        "long_volume",
        "long_ratio",
    ]
    long = con.execute(
        "SELECT long_volume, long_ratio FROM finra_short_volume "
        "WHERE symbol = 'A' AND date = DATE '2026-09-30'"
    ).fetchone()
    assert long == (1.0, 0.5)  # total 2, short 1
    rows = con.execute(
        "SELECT symbol, security_kind, eodhd_code, short_ratio FROM finra_short_volume "
        "WHERE date = DATE '2026-09-30' ORDER BY symbol"
    ).fetchall()
    assert len(rows) == len(SYMBOLS)
    # the SQL mapping agrees with the Python one on every spelling
    for symbol, kind, code, ratio in rows:
        assert kind == sv.security_kind(symbol), symbol
        assert code == sv.eodhd_code(symbol), symbol
        assert ratio == 0.5
    by_symbol = {r[0]: r for r in rows}
    assert by_symbol["BRK/B"][1:3] == ("common", "BRK-B")
    assert by_symbol["ABRpD"][1:3] == ("preferred", None)
    assert by_symbol["AACT/WS"][1:3] == ("warrant", None)
    # both days are unioned; a date predicate filters
    assert con.execute("SELECT count(*) FROM finra_short_volume").fetchone()[
        0
    ] == 2 * len(SYMBOLS)
    assert (
        con.execute(
            "SELECT count(DISTINCT date) FROM finra_short_volume WHERE date >= DATE '2026-09-30'"
        ).fetchone()[0]
        == 1
    )


def test_state_view_and_zero_total(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    store = sv.store(tmp_path)
    day = date(2026, 9, 30)
    frame = pd.DataFrame(
        {
            "date": [day],
            "symbol": ["ZERO"],
            "short_volume": [0.0],
            "short_exempt_volume": [0.0],
            "total_volume": [0.0],
            "facilities": ["Q"],
            "source": ["cdn"],
        }
    )
    store.write_day(day, frame)
    store.upsert_state(
        [
            DayState("2026-09-30", "ok", "cdn", rows=1),
            DayState("2026-09-29", "absent", "cdn"),
        ]
    )
    con = duckdb.connect()
    views.register(con, root=tmp_path)
    assert con.execute("SELECT short_ratio FROM finra_short_volume").fetchone() == (
        None,
    )
    state = con.execute(
        "SELECT date, status FROM finra_short_volume_state ORDER BY date"
    ).fetchall()
    assert state == [("2026-09-29", "absent"), ("2026-09-30", "ok")]


def test_schema_snippet_names_both_views() -> None:
    snippet = views.schema_snippet()
    assert "finra_short_volume(date, symbol" in snippet
    assert "finra_short_volume_state(" in snippet
    assert "eodhd_code" in snippet and "short_ratio" in snippet


def test_lab_schema_text_includes_finra_when_present(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    from lab import data as lab_data

    con = duckdb.connect()
    assert "FINRA views" not in lab_data.schema_text(con)
    _seed(tmp_path, date(2026, 9, 30))
    views.register(con, root=tmp_path)
    assert "FINRA views" in lab_data.schema_text(con)


def test_explorer_connect_registers_finra(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The eodhd explorer's connection carries the FINRA view once data exists."""
    pytest.importorskip("duckdb")
    monkeypatch.setenv("DATACLI_FINRA_ROOT", str(tmp_path))
    _seed(tmp_path, date(2026, 9, 30))
    sys.path.insert(0, str(_REPO_ROOT / "eodhd"))
    import explore_eodhd  # type: ignore[import-not-found]

    con = explore_eodhd.connect()
    assert con.execute("SELECT count(*) FROM finra_short_volume").fetchone()[0] == len(
        SYMBOLS
    )


def test_weekly_flow_views_and_dot_spelled_classes(tmp_path: Path) -> None:
    duckdb = pytest.importorskip("duckdb")
    from datetime import date as _date

    from finra import weekly_flow as wf

    rows = []
    for symbol in ("AAPL", "BRK.B", "BF.A", "AACT.WS", "ABRpD"):
        for kind, mpid in (
            ("ATS_W_SMBL", None),
            ("OTC_W_SMBL", None),
            ("ATS_W_SMBL_FIRM", "MSPL"),
        ):
            rows.append(
                {
                    "weekStartDate": "2026-09-07",
                    "tierIdentifier": "T1",
                    "summaryTypeCode": kind,
                    "issueSymbolIdentifier": symbol,
                    "issueName": "n",
                    "MPID": mpid,
                    "firmCRDNumber": None,
                    "marketParticipantName": None,
                    "productTypeCode": None,
                    "totalWeeklyTradeCount": 10,
                    "totalWeeklyShareQuantity": 100.5,
                    "totalNotionalSum": 1000.0,
                    "initialPublishedDate": "2026-09-28",
                    "lastUpdateDate": "2026-09-28",
                    "lastReportedDate": "2026-09-11",
                }
            )
    frame = wf.frame_from_api_rows(_date(2026, 9, 7), "T1", rows)
    store = wf.store(tmp_path)
    store.write_day(_date(2026, 9, 7), frame, part="T1")
    con = duckdb.connect()
    assert views.register(con, root=tmp_path)["finra_weekly_flow"] is True
    got = con.execute(
        "SELECT symbol, security_kind, eodhd_code, ats_shares, otc_trades, published_at "
        "FROM finra_weekly_flow_symbol ORDER BY symbol"
    ).fetchall()
    assert len(got) == 5
    for symbol, kind, code, ats, otc_trades, published in got:
        assert kind == wf.security_kind(symbol) and code == wf.eodhd_code(
            symbol
        ), symbol
        assert ats == 100.5 and otc_trades == 10 and str(published) == "2026-09-28"
    by = {g[0]: g for g in got}
    assert by["BRK.B"][1:3] == ("common", "BRK-B") and by["BF.A"][2] == "BF-A"
    assert by["AACT.WS"][1] == "warrant" and by["ABRpD"][1] == "preferred"
    # the raw view keeps every row kind; the FIRM rows are excluded from the symbol pivot
    assert con.execute("SELECT count(*) FROM finra_weekly_flow").fetchone()[0] == 15
    snippet = views.schema_snippet(short_volume=False)
    assert (
        "finra_weekly_flow_symbol(" in snippet and "finra_short_volume(" not in snippet
    )
