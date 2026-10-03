"""registers.eu and registers.ods: the five other registers' parsers on small fixtures, and multi-part payloads."""

from __future__ import annotations

import datetime as dt
import io
import sys
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registers import common, eu, ods  # noqa: E402

NL_HIST = (
    '"Positie houder";"Naam van de emittent";"ISIN";"Netto Shortpositie";"Positiedatum"\n'
    '"GSA Capital Partners LLP";"Pharming Group N.V.";"NL0010391025";"0,48";"2026-10-01 00:00:00"\n'
    '"Citadel Advisors LLC";"Koninklijke Ahold Delhaize N.V.";"NL0011794037";"0,61";"2026-09-30 00:00:00"\n'
).encode("latin-1")
NL_CUR = (
    '"Positie houder";"Naam van de emittent";"ISIN";"Netto Shortpositie";"Positiedatum"\n'
    '"Citadel Advisors LLC";"Koninklijke Ahold Delhaize N.V.";"NL0011794037";"0,61";"2026-09-30 00:00:00"\n'
    '"Marshall Wace LLP";"Société Générale";"FR0000130809";"0,52";"2026-10-02 00:00:00"\n'
    '"Disclaimer CSV";"";"";"";""\n'
    '"Datum laatste update 03 Oct 2026";"";"";"";""\n'
).encode("latin-1")


def _ods(rows: list[list[str | None]]) -> bytes:
    cells = []
    for row in rows:
        parts = []
        for v in row:
            if v is None:
                parts.append('<table:table-cell/>')
            else:
                parts.append(f'<table:table-cell office:value-type="string"><text:p>{escape(v)}</text:p></table:table-cell>')
        cells.append("<table:table-row>" + "".join(parts) + "</table:table-row>")
    content = (
        '<?xml version="1.0" encoding="UTF-8"?><office:document-content '
        'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:spreadsheet>'
        '<table:table table:name="Blad1">' + "".join(cells) + "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        z.writestr("content.xml", content)
    return buffer.getvalue()


SE_ROWS = [
    ["Historiska positioner"],
    ["Betydande korta nettopositioner i aktier (blankning)", None, None, "fi.se/blankning"],
    [None, "rapportering@fi.se"],
    ["Innehavare av positionen", "Namn på emittent", "ISIN", "Position i procent", "Datum för positionen", "Kommentar"],
    ["Luxor Capital Group LP", "BillerudKorsnäs Aktiebolag (publ)\xa0", "SE0000862997", "0.62", "2010-05-10T00:00:00"],
    ["AQR Capital Management, LLC", "Volvo AB", "SE0000115446", "0.49", "2026-10-01"],
    ["MILLENNIUM INTERNATIONAL MANAGEMENT LP", "SSAB AB", None, "<0,5", "2019-05-31"],  # no ISIN: dropped
    ["Lucerne Capital Management, L.P.", "Munters Group AB", "SE0009806607", "<0,5", "2019-02-06T00:00:00"],  # below the threshold: 0.0
]

NO_JSON = b"""[
 {"isin": "NO0010096985", "issuerName": "EQUINOR", "events": [
   {"date": "2026-09-01T00:00:00", "shortPercent": 1.1, "activePositions": [
     {"date": "2026-09-01T00:00:00", "shortPercent": 0.6, "positionHolder": "A LLP"},
     {"date": "2026-08-15T00:00:00", "shortPercent": 0.5, "positionHolder": "B LP"}]},
   {"date": "2026-09-10T00:00:00", "shortPercent": 0.6, "activePositions": [
     {"date": "2026-09-01T00:00:00", "shortPercent": 0.6, "positionHolder": "A LLP"}]},
   {"date": "2026-09-20T00:00:00", "shortPercent": 0, "activePositions": []}]}
]"""


def test_nl_parse_unions_the_two_exports_and_reads_decimal_commas() -> None:
    parsed = eu.parse_nl({"history": NL_HIST, "current": NL_CUR})
    rows = parsed.rows
    assert len(rows) == 3 and parsed.dropped == 2  # the Ahold row appears in both exports once; the trailer is dropped
    assert set(rows["market"]) == {"nl"} and parsed.file_date == dt.date(2026, 10, 2)
    ahold = rows[rows["isin"] == "NL0011794037"].iloc[0]
    assert ahold["net_short_pct"] == pytest.approx(0.61) and ahold["position_date"] == dt.date(2026, 9, 30)
    assert ahold["published_from"] == dt.date(2026, 10, 1) and ahold["published_to"] is None
    assert rows[rows["isin"] == "FR0000130809"].iloc[0]["issuer"] == "Société Générale"
    with pytest.raises(common.RegisterError, match="unexpected columns"):
        eu.parse_nl({"history": b"a;b\n1;2\n"})


def test_se_parse_reads_the_ods_files_after_the_title_rows() -> None:
    parsed = eu.parse_se({"history": _ods(SE_ROWS), "current": _ods(SE_ROWS[:4] + [SE_ROWS[5]])})
    rows = parsed.rows
    assert len(rows) == 3 and parsed.file_date == dt.date(2026, 10, 1) and parsed.dropped == 1
    assert rows[rows["isin"] == "SE0009806607"].iloc[0]["net_short_pct"] == 0.0
    luxor = rows[rows["holder"] == "Luxor Capital Group LP"].iloc[0]
    assert luxor["position_date"] == dt.date(2010, 5, 10) and luxor["issuer"] == "BillerudKorsnäs Aktiebolag (publ)"
    assert luxor["net_short_pct"] == pytest.approx(0.62) and luxor["published_from"] == dt.date(2010, 5, 11)
    with pytest.raises(common.RegisterError, match="header row"):
        eu.parse_se({"history": _ods(SE_ROWS[:3])})
    with pytest.raises(common.RegisterError, match="OpenDocument"):
        eu.parse_se({"history": b"junk"})
    assert ods.sheet_rows(_ods([["a", None, "b"], [None], ["c"]])) == [["a", None, "b"], ["c"]]


def test_norway_rows_close_a_holder_at_the_next_event() -> None:
    parsed = eu.parse_no({"api": NO_JSON})
    rows = parsed.rows.sort_values(["holder", "position_date"]).reset_index(drop=True)
    a = rows[rows["holder"] == "A LLP"]
    b = rows[rows["holder"] == "B LP"]
    assert a["net_short_pct"].tolist() == [0.6, 0.0] and a["position_date"].tolist() == [dt.date(2026, 9, 1), dt.date(2026, 9, 20)]
    assert b["net_short_pct"].tolist() == [0.5, 0.0] and b["position_date"].tolist() == [dt.date(2026, 8, 15), dt.date(2026, 9, 10)]
    assert set(rows["issuer"]) == {"EQUINOR"} and parsed.file_date == dt.date(2026, 9, 20)
    with pytest.raises(common.RegisterError, match="no instruments"):
        eu.parse_no({"api": b"[]"})


def test_ie_parse_reads_both_sheets_from_the_second_column() -> None:
    def sheet(rows: list[list]) -> pd.DataFrame:
        head = ["Table of Current Significant Net Short Positions in Shares", *eu.IE_COLUMNS, "Comments:"]
        return pd.DataFrame([head, *rows])

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        sheet([[None, "Millennium International Management LP", "RYANAIR HOLDINGS PLC", "IE00BYTBXV33", 0.52, pd.Timestamp("2026-09-29"), None]]).to_excel(writer, sheet_name="Current", index=False, header=False)
        sheet([[None, "Millennium International Management LP", "RYANAIR HOLDINGS PLC", "IE00BYTBXV33", 0, pd.Timestamp("2026-05-29"), None],
               [None, None, None, None, None, None, None]]).to_excel(writer, sheet_name="Historical", index=False, header=False)
    parsed = eu.parse_ie({"workbook": buffer.getvalue()})
    rows = parsed.rows.sort_values("position_date")
    assert rows["net_short_pct"].tolist() == [0.0, 0.52] and rows["published_from"].tolist()[1] == dt.date(2026, 9, 30)
    assert parsed.file_date == dt.date(2026, 9, 29)


def test_de_parse_reads_the_comma_separated_history_and_current() -> None:
    current = '﻿"Positionsinhaber","Emittent","ISIN","Position","Datum"\n"AKO Capital LLP","Bechtle Aktiengesellschaft","DE0005158703","0,54","2026-10-01"\n'.encode("utf-8")
    history = current + '"Tiger Global Investments, L.P.","SolarWorld Aktiengesellschaft","DE0005108401","0,72","2012-10-26"\n'.encode("utf-8")
    parsed = eu.parse_de({"current": current, "history": history})
    assert len(parsed.rows) == 2 and parsed.rows["net_short_pct"].tolist() == [0.54, 0.72]
    assert parsed.rows["position_date"].tolist() == [dt.date(2026, 10, 1), dt.date(2012, 10, 26)]


def test_refresh_digests_multi_part_payloads(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 3, tzinfo=dt.timezone.utc)  # noqa: E731
    assert common.digest({"a": b"1", "b": b"2"}) == common.digest({"b": b"2", "a": b"1"}) != common.digest({"a": b"12"})
    assert common.size({"a": b"1", "b": b"22"}) == 3 and common.size(b"abc") == 3
    report = common.refresh("nl", lambda: ({"history": NL_HIST, "current": NL_CUR}, "afm"), eu.parse_nl, tmp_path, run=True, now=clock)
    assert report.outcome == "stored" and report.rows == 3
    state = common.store(tmp_path, "nl").load_state()
    assert state["bytes"] == len(NL_HIST) + len(NL_CUR)
    again = common.refresh("nl", lambda: ({"history": NL_HIST, "current": NL_CUR}, "afm"), eu.parse_nl, tmp_path, run=True, now=clock)
    assert again.outcome == "unchanged"
    assert set(common.MARKETS) == {"uk", "fr", "nl", "se", "no", "ie", "de"} and "uk" not in common.LIVE_MARKETS
