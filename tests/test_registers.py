"""registers: the FCA and AMF parsers, the history store's refresh rule, status, qc and the views."""

from __future__ import annotations

import datetime as dt
import io
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registers import amf, common, fca, views  # noqa: E402


def _fca_workbook(rows: list[tuple], sheet: str = "Historic Disclosures 10.07.2026") -> bytes:
    frame = pd.DataFrame(rows, columns=list(fca.COLUMNS))
    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name=sheet, index=False)
    return buffer.getvalue()


FCA_ROWS = [
    ("AlphaGen Capital Limited", "1SPATIAL PLC", "GB00B09LQS34", 0.71, pd.Timestamp("2013-05-24")),
    ("AlphaGen Capital Limited", "1SPATIAL PLC", "GB00B09LQS34", 1.42, pd.Timestamp("2013-05-29")),
    ("AlphaGen Capital Limited", "1SPATIAL PLC", "GB00B09LQS34", 0.00, pd.Timestamp("2013-06-24")),  # a Monday
    ("Marshall Wace LLP", "3I GROUP PLC", "GB00B1YW4409", 0.52, pd.Timestamp("2026-07-10")),  # a Friday
]

AMF_CSV = (
    '﻿"Detenteur de la position courte nette";"Legal Entity Identifier detenteur";"Emetteur / issuer";"Ratio";"code ISIN";'
    '"Date de debut position";"Date de debut de publication position";"Date de fin de publication position"\n'
    '"12 WEST CAPITAL MANAGEMENT LP";"549300CTKKI0FKNCD259";"UBISOFT ENTERTAINMENT";"0.35";"FR0000054470";"2015-10-06";"2015-10-07";"2015-10-08"\n'
    '"12 WEST CAPITAL MANAGEMENT LP";"549300CTKKI0FKNCD259";"UBISOFT ENTERTAINMENT";"1.17";"FR0000054470";"2015-10-05";"2015-10-06";"2015-10-08"\n'
    '"MARSHALL WACE LLP";"";"ALSTOM";"0.61";"FR0010220475";"2026-09-30";"2026-10-01";""\n'
).encode("utf-8")


def test_fca_parse_is_strict_and_dates_the_publication_on_the_next_weekday() -> None:
    parsed = fca.parse(_fca_workbook(FCA_ROWS))
    assert parsed.file_date == dt.date(2026, 7, 10)
    rows = parsed.rows
    assert list(rows.columns) == list(common.COLUMNS) and len(rows) == 4
    assert set(rows["market"]) == {"uk"} and rows["holder_lei"].isna().all() and rows["published_to"].isna().all()
    first = rows.iloc[0]
    assert first["holder"] == "AlphaGen Capital Limited" and first["position_date"] == dt.date(2013, 5, 24)
    assert first["published_from"] == dt.date(2013, 5, 27)  # Friday -> Monday
    assert rows.iloc[2]["net_short_pct"] == 0.0 and rows.iloc[2]["published_from"] == dt.date(2013, 6, 25)
    assert rows.iloc[3]["published_from"] == dt.date(2026, 7, 13)
    with pytest.raises(common.RegisterError, match="one 'Historic Disclosures"):
        fca.parse(_fca_workbook(FCA_ROWS, sheet="Current Disclosures"))
    with pytest.raises(common.RegisterError, match="unexpected columns"):
        bad = pd.DataFrame(FCA_ROWS, columns=["a", "b", "c", "d", "e"])
        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            bad.to_excel(writer, sheet_name="Historic Disclosures 01.01.2026", index=False)
        fca.parse(buffer.getvalue())
    with pytest.raises(common.RegisterError, match="not a readable workbook"):
        fca.parse(b"not an xlsx")


def test_amf_parse_reads_the_three_dates_and_the_lei() -> None:
    parsed = amf.parse(AMF_CSV, file_date=dt.date(2026, 10, 2))
    rows = parsed.rows
    assert parsed.file_date == dt.date(2026, 10, 2) and len(rows) == 3 and set(rows["market"]) == {"fr"}
    alstom = rows[rows["isin"] == "FR0010220475"].iloc[0]
    assert alstom["holder_lei"] is None and alstom["published_to"] is None and alstom["net_short_pct"] == 0.61
    assert alstom["published_from"] == dt.date(2026, 10, 1)
    ubi = rows[rows["isin"] == "FR0000054470"].sort_values("position_date")
    assert ubi["published_to"].tolist() == [dt.date(2015, 10, 8)] * 2 and ubi["holder_lei"].iloc[0] == "549300CTKKI0FKNCD259"
    # without a file date the latest publication start stands in
    assert amf.parse(AMF_CSV).file_date == dt.date(2026, 10, 1)
    with pytest.raises(common.RegisterError, match="unexpected columns"):
        amf.parse(b"a;b\n1;2\n")


def test_amf_latest_resource_takes_the_newest_csv_and_dates_it_from_the_name() -> None:
    payload = {
        "resources": [
            {"format": "csv", "url": "https://x/export_od_vad_20261001111500_1.csv", "last_modified": "2026-10-01T10:30:03+00:00"},
            {"format": "csv", "url": "https://x/export_od_vad_20261002111500_2.csv", "last_modified": "2026-10-02T10:30:03+00:00"},
            {"format": "pdf", "url": "https://x/notice.pdf", "last_modified": "2026-10-03T00:00:00+00:00"},
        ]
    }
    url, day = amf.latest_resource(payload)
    assert url.endswith("_2.csv") and day == dt.date(2026, 10, 2)
    assert amf.file_date_of(url) == dt.date(2026, 10, 2) and amf.file_date_of("https://x/other.csv") is None
    with pytest.raises(common.RegisterError, match="no CSV"):
        amf.latest_resource({"resources": [{"format": "pdf", "url": "u"}]})


def test_refresh_stores_then_is_idempotent_replaces_on_change_and_refuses_a_shrunk_history(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 3, 18, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    first = _fca_workbook(FCA_ROWS[:3])
    planned = common.refresh("uk", lambda: (first, "u"), fca.parse, tmp_path, run=False, now=clock)
    assert planned.outcome == "planned" and planned.rows == 3 and not common.store(tmp_path, "uk").exists()
    stored = common.refresh("uk", lambda: (first, "u"), fca.parse, tmp_path, run=True, now=clock)
    assert stored.outcome == "stored" and common.store(tmp_path, "uk").exists()
    state = common.store(tmp_path, "uk").load_state()
    assert state["rows"] == 3 and state["file_date"] == "2026-07-10" and state["fetched_at"] == "2026-10-03T18:00:00Z"
    again = common.refresh("uk", lambda: (first, "u"), fca.parse, tmp_path, run=True, now=clock)
    assert again.outcome == "unchanged"
    bigger = _fca_workbook(FCA_ROWS, sheet="Historic Disclosures 11.07.2026")
    replaced = common.refresh("uk", lambda: (bigger, "u"), fca.parse, tmp_path, run=True, now=clock)
    assert replaced.outcome == "replaced" and "replaced the stored file of 2026-07-10" in replaced.detail
    assert common.store(tmp_path, "uk").previous.exists() and len(pd.read_parquet(common.store(tmp_path, "uk").previous)) == 3
    assert len(common.store(tmp_path, "uk").read()) == 4
    shrunk = common.refresh("uk", lambda: (first, "u"), fca.parse, tmp_path, run=True, now=clock)
    assert shrunk.outcome == "failed" and "must not shrink" in shrunk.detail
    assert len(common.store(tmp_path, "uk").read()) == 4  # untouched
    broken = common.refresh("uk", lambda: (b"junk", "u"), fca.parse, tmp_path, run=True, now=clock)
    assert broken.outcome == "failed" and "RegisterError" in broken.detail
    with pytest.raises(common.RegisterError, match="unknown market"):
        common.store(tmp_path, "xx")


def test_status_qc_and_views_over_two_markets(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)  # noqa: E731
    assert common.qc(tmp_path) == [("warn", "empty", "no register history stored; run `registers fetch --run`")]
    assert all(not e["present"] for e in common.status(tmp_path))
    common.refresh("uk", lambda: (_fca_workbook(FCA_ROWS), "fca"), fca.parse, tmp_path, run=True, now=clock)
    common.refresh("fr", lambda: (AMF_CSV, "amf"), lambda d: amf.parse(d, file_date=dt.date(2026, 10, 2)), tmp_path, run=True, now=clock)
    entries = {e["market"]: e for e in common.status(tmp_path)}
    assert entries["uk"]["rows"] == 4 and entries["fr"]["rows"] == 3 and entries["fr"]["file_date"] == "2026-10-02"
    findings = common.qc(tmp_path, today=dt.date(2026, 10, 3))
    real = [f for f in findings if not f[1].endswith("_empty")]
    assert [f[1] for f in real] == ["uk_stale"] and real[0][0] == "warn"  # the frozen FCA file; the AMF file is one day old
    assert {f[1] for f in findings} - {"uk_stale"} == {f"{m}_empty" for m in common.MARKETS if m not in ("uk", "fr")}
    con = duckdb.connect()
    assert views.register(con, root=tmp_path) == {views.VIEW: True}
    table = con.execute(f"SELECT market, count(*) AS n FROM {views.VIEW} GROUP BY 1 ORDER BY 1").fetchall()
    assert table == [("fr", 3), ("uk", 4)]
    visible = con.execute(f"SELECT count(*) FROM {views.VIEW} WHERE published_from <= DATE '2015-10-06'").fetchone()[0]
    assert visible == 1 + 3  # one AMF row published by then, the three 2013 FCA rows
    state = con.execute(f"SELECT market, rows FROM {views.STATE_VIEW} ORDER BY 1").fetchall()
    assert state == [("fr", 3), ("uk", 4)]
    assert views.register(duckdb.connect(), root=tmp_path / "nowhere") == {views.VIEW: False}
    assert "short_register(" in views.schema_snippet()


def test_next_weekday() -> None:
    assert common.next_weekday(dt.date(2026, 10, 2)) == dt.date(2026, 10, 5)  # Friday -> Monday
    assert common.next_weekday(dt.date(2026, 10, 3)) == dt.date(2026, 10, 5)  # Saturday -> Monday
    assert common.next_weekday(dt.date(2026, 10, 5)) == dt.date(2026, 10, 6)
