"""registers.holdings: the AFM substantial-holdings export and the issued-capital register on small fixtures."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from registers import holdings  # noqa: E402

HEADER = (
    "Datum meldingsplicht;Uitgevende instelling;Meldingsplichtige;Kvk-nr;Plaats;Soort aandeel;Kapitaalbelang;Stemrecht;"
    "Wijze van beschikken;Aantal aandelen;Aantal stemmen;Aantal equivalente aandelen;Soort aandeel ENG;Toelichting;"
    "Soort aandeel procentuele verdeling;Totale deelneming;Rechtstreeks reëel;Rechtstreeks potentieel;Middellijk reëel;"
    "Middellijk potentieel;Totaal kapitaalbelang;Rechtstreeks;Middellijk\n"
)


def _line(date, issuer, holder, instrument_nl, kind, disposal, shares, votes, instrument, breakdown, total, dr, dp, ir, ip, kvk="", seat="Londen"):
    return ";".join(
        [date, issuer, holder, kvk, seat, instrument_nl, kind, kind, disposal, shares, votes, "", instrument, "", breakdown, total, dr, dp, ir, ip, "", "", ""]
    ) + "\n"


def _fixture() -> bytes:
    rows = [HEADER]
    # Weiss / Allfunds: two instrument lines (ordinary shares real, swap potential), each with a capital and a voting breakdown;
    # the ordinary-share line is repeated once (the export's duplicates).
    for breakdown, total, ip in (("Kapitaalbelang", "3,09 %", "3,09 %"), ("Stemrecht", "0,00 %", "0,00 %")):
        rows.append(_line("2026-10-01 00:00:00", "Allfunds Group plc", "Weiss Asset Management LP", "Gewoon aandeel", "Reëel",
                          "Middellijk<BR>(Brookdale International Partners, L.P.)", "100.00000", "100.00000", "Ordinary share", breakdown, total, "0,00 %", "0,00 %", "0,00 %", ip))
        rows.append(_line("2026-10-01 00:00:00", "Allfunds Group plc", "Weiss Asset Management LP", "Gewoon aandeel", "Reëel",
                          "Middellijk<BR>(Brookdale International Partners, L.P.)", "100.00000", "100.00000", "Ordinary share", breakdown, total, "0,00 %", "0,00 %", "0,00 %", ip))
        rows.append(_line("2026-10-01 00:00:00", "Allfunds Group plc", "Weiss Asset Management LP", "Total equity return swap", "Potentieel",
                          "Middellijk<BR>(Brookdale International Partners, L.P.)", "18608189.00000", "0.00000", "Total equity return swap", breakdown, total, "0,00 %", "0,00 %", "0,00 %", ip))
    # Goldman / Signify: one direct real line, 5,13 % total of which 2,23 % real.
    for breakdown in ("Kapitaalbelang", "Stemrecht"):
        rows.append(_line("2026-09-30 00:00:00", "Signify N.V.", "Goldman Sachs Group Inc., The", "Gewoon aandeel", "Reëel", "Rechtstreeks",
                          "2501678.00000", "2501678.00000", "Ordinary share", breakdown, "5,13 %", "2,23 %", "2,90 %", "0,00 %", "0,00 %", kvk="12345678", seat="New York"))
    # a stray row without a breakdown kind
    rows.append(_line("2026-09-30 00:00:00", "Signify N.V.", "Goldman Sachs Group Inc., The", "Gewoon aandeel", "Reëel", "Rechtstreeks",
                      "1.00000", "1.00000", "Ordinary share", "", "", "", "", "", ""))
    return "".join(rows).encode("latin-1")


def test_parse_holdings_merges_the_two_breakdowns_drops_duplicates_and_reads_the_percentages() -> None:
    parsed = holdings.parse_holdings(_fixture())
    lines = parsed.lines
    assert list(lines.columns) == list(holdings.LINE_COLUMNS)
    assert len(lines) == 3 and parsed.duplicates == 1 and parsed.dropped == 1 and parsed.inconsistent_totals == 0
    weiss = lines[lines["holder"] == "Weiss Asset Management LP"].sort_values("instrument").reset_index(drop=True)
    assert weiss["obligation_date"].tolist() == [dt.date(2026, 10, 1)] * 2
    assert weiss["real_or_potential"].tolist() == ["real", "potential"]
    assert weiss["direct_or_indirect"].tolist() == ["indirect", "indirect"]
    assert weiss["via"].tolist() == ["Brookdale International Partners, L.P."] * 2
    assert weiss["shares"].tolist() == [100.0, 18608189.0]
    assert weiss["pct_capital"].tolist() == [3.09, 3.09] and weiss["pct_voting"].tolist() == [0.0, 0.0]
    assert weiss["pct_capital_indirect_potential"].tolist() == [3.09, 3.09]
    gs = lines[lines["holder"].str.startswith("Goldman")].iloc[0]
    assert gs["direct_or_indirect"] == "direct" and gs["via"] == "" and gs["holder_kvk"] == "12345678" and gs["holder_seat"] == "New York"
    assert gs["pct_capital"] == 5.13 and gs["pct_capital_direct_real"] == 2.23 and gs["pct_capital_direct_potential"] == 2.90


def test_notifications_fold_lines_to_one_row_with_real_and_potential_totals() -> None:
    notes = holdings.notifications(holdings.parse_holdings(_fixture()).lines)
    assert list(notes.columns) == list(holdings.NOTIFICATION_COLUMNS)
    assert len(notes) == 2
    weiss = notes[notes["holder"] == "Weiss Asset Management LP"].iloc[0]
    assert weiss["n_lines"] == 2 and weiss["instruments"] == "Ordinary share|Total equity return swap"
    assert bool(weiss["has_potential"]) is True
    assert weiss["pct_capital"] == 3.09 and weiss["pct_capital_real"] == 0.0 and weiss["pct_capital_potential"] == 3.09
    assert weiss["shares_real"] == 100.0 and weiss["shares_potential"] == 18608189.0
    gs = notes[notes["holder"].str.startswith("Goldman")].iloc[0]
    assert gs["pct_capital_real"] == 2.23 and gs["pct_capital_potential"] == pytest.approx(2.90) and bool(gs["has_potential"]) is False
    assert holdings.notifications(notes.iloc[0:0]).empty


def test_parse_holdings_refuses_another_header() -> None:
    with pytest.raises(holdings.HoldingsFormatError):
        holdings.parse_holdings(b"a;b;c\n1;2;3\n")
    bad = HEADER.replace("Meldingsplichtige", "Melder").encode("latin-1")
    with pytest.raises(holdings.HoldingsFormatError):
        holdings.parse_holdings(bad)


DE_SNAPSHOT = (
    "﻿SFC Energy AG;Brunnthal;Deutschland;1752748 Alberta Ltd.;Calgary;Kanada;3,59;;;01.10.2013\n"
    "2invest AG;Heidelberg;Deutschland;2invest AG;Heidelberg;Deutschland;3,04;0,0;0,0;16.07.2024\n"
    "3U Holding AG;Marburg;Deutschland;3U Holding AG;Marburg;Deutschland;8,82;0,0;8,82;13.11.2023\n"
    "3U Holding AG;Marburg;Deutschland;3U Holding AG;Marburg;Deutschland;8,82;0,0;8,82;13.11.2023\n"
).encode("utf-8")
DE_SNAPSHOT_2 = (
    "﻿2invest AG;Heidelberg;Deutschland;2invest AG;Heidelberg;Deutschland;3,04;0,0;0,0;16.07.2024\n"
    "3U Holding AG;Marburg;Deutschland;3U Holding AG;Marburg;Deutschland;8,82;0,0;8,82;13.11.2023\n"
    "3U Holding AG;Marburg;Deutschland;Citadel Advisors LLC;Chicago;USA;3,10;0,0;3,10;02.10.2026\n"
).encode("utf-8")


def test_parse_de_reads_the_headerless_snapshot() -> None:
    frame = holdings.parse_de(DE_SNAPSHOT)
    assert list(frame.columns) == list(holdings.DE_COLUMNS) and len(frame) == 3  # the duplicate dropped
    sfc = frame.iloc[0]
    assert sfc["holder"] == "1752748 Alberta Ltd." and sfc["pct_voting"] == 3.59 and pd.isna(sfc["pct_instruments"]) and sfc["published_at"] == dt.date(2013, 10, 1)
    assert frame.iloc[2]["pct_total"] == 8.82
    with pytest.raises(holdings.HoldingsFormatError):
        holdings.parse_de(b"a;b;c\n")


def test_visible_from_adds_two_weekdays_over_weekends() -> None:
    dates = pd.Series([dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 3), dt.date(2026, 10, 5)])  # Thu, Fri, Sat, Mon
    assert holdings.visible_from(dates).tolist() == [dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 6), dt.date(2026, 10, 7)]


def test_refresh_holdings_nl_accumulates_with_first_seen_and_never_shrinks(tmp_path: Path) -> None:
    capital = (
        "Datum meldingsplicht;Uitgevende onderneming;Inschrijving handelsregister;Plaats;Totaal geplaatst kapitaal;Totaal aantal stemmen;Aantal gecertificeerd\n"
        "2026-09-30 00:00:00;Signify N.V.;65220692;Eindhoven;1000.0;1000.0;0.0\n"
    ).encode("latin-1")
    day1 = lambda: ({"holdings": _fixture(), "capital": capital}, "fake", dt.date(2026, 10, 5))  # noqa: E731
    planned = holdings.refresh_holdings("nl", day1, tmp_path, run=False)
    assert planned.outcome == "planned" and planned.rows == 2 and not holdings.HoldingsStore(tmp_path, "nl").exists()
    stored = holdings.refresh_holdings("nl", day1, tmp_path, run=True)
    assert stored.outcome == "stored" and stored.rows == 2 and stored.file_date == dt.date(2026, 10, 5)
    store = holdings.HoldingsStore(tmp_path, "nl")
    frame = store.read()
    assert frame is not None and set(frame["first_seen"]) == {dt.date(2026, 10, 5)} and frame["last_seen"].isna().all()
    assert "published_from" in frame.columns and (store.dir / "lines.parquet").exists() and (store.dir / "capital.parquet").exists()
    assert pd.read_parquet(store.dir / "capital.parquet")["issuer_kvk"].tolist() == ["65220692"]
    assert holdings.refresh_holdings("nl", day1, tmp_path, run=True).outcome == "unchanged"
    # day 2: one more notification, one of the old ones gone from the export
    extra = _line("2026-10-02 00:00:00", "Signify N.V.", "Citadel Advisors LLC", "Gewoon aandeel", "Reëel", "Rechtstreeks",
                  "1.00000", "1.00000", "Ordinary share", "Kapitaalbelang", "3,10 %", "3,10 %", "0,00 %", "0,00 %", "0,00 %")
    body = _fixture().decode("latin-1").splitlines(keepends=True)
    without_goldman = "".join(line for line in body if "Goldman" not in line) + extra
    day2 = lambda: ({"holdings": without_goldman.encode("latin-1"), "capital": capital}, "fake", dt.date(2026, 10, 6))  # noqa: E731
    replaced = holdings.refresh_holdings("nl", day2, tmp_path, run=True)
    assert replaced.outcome == "replaced" and replaced.rows == 3 and "1 new rows, 1 closed" in replaced.detail
    frame = store.read()
    assert frame is not None and len(frame) == 3
    by_holder = frame.set_index("holder")
    assert by_holder.loc["Citadel Advisors LLC", "first_seen"] == dt.date(2026, 10, 6) and by_holder.loc["Citadel Advisors LLC", "published_from"] == dt.date(2026, 10, 6)
    assert by_holder.loc["Goldman Sachs Group Inc., The", "last_seen"] == dt.date(2026, 10, 6)
    assert pd.isna(by_holder.loc["Weiss Asset Management LP", "last_seen"])
    entries = {e["market"]: e for e in holdings.status_holdings(tmp_path)}
    assert entries["nl"]["rows"] == 3 and entries["nl"]["file_date"] == "2026-10-06" and not entries["de"]["present"]
    failed = holdings.refresh_holdings("nl", lambda: ({"holdings": b"x;y\n", "capital": capital}, "bad"), tmp_path, run=True)
    assert failed.outcome == "failed" and "HoldingsFormatError" in failed.detail


def test_refresh_holdings_de_closes_holdings_that_leave_the_snapshot(tmp_path: Path) -> None:
    clock = lambda: dt.datetime(2026, 10, 5, 23, 0, tzinfo=dt.timezone.utc)  # noqa: E731
    first = holdings.refresh_holdings("de", lambda: ({"snapshot": DE_SNAPSHOT}, "fake"), tmp_path, run=True, now=clock)
    assert first.outcome == "stored" and first.rows == 3 and first.file_date == dt.date(2026, 10, 5)
    second = holdings.refresh_holdings("de", lambda: ({"snapshot": DE_SNAPSHOT_2}, "fake"), tmp_path, run=True, today=dt.date(2026, 10, 6), now=clock)
    assert second.outcome == "replaced" and second.rows == 4
    frame = holdings.HoldingsStore(tmp_path, "de").read()
    assert frame is not None
    sfc = frame[frame["issuer"] == "SFC Energy AG"].iloc[0]
    assert sfc["last_seen"] == dt.date(2026, 10, 6) and sfc["first_seen"] == dt.date(2026, 10, 5)
    citadel = frame[frame["holder"] == "Citadel Advisors LLC"].iloc[0]
    assert citadel["first_seen"] == dt.date(2026, 10, 6) and pd.isna(citadel["last_seen"]) and citadel["published_at"] == dt.date(2026, 10, 2)
    assert frame["last_seen"].notna().sum() == 1


def test_read_dir_reads_saved_exports(tmp_path: Path) -> None:
    (tmp_path / "bafin_gesamtexport.csv").write_bytes(DE_SNAPSHOT)
    payload, source, file_date = holdings.read_dir("de", tmp_path)
    assert payload == {"snapshot": DE_SNAPSHOT} and "saved exports" in source and file_date == dt.date.today() or file_date <= dt.date.today()
    with pytest.raises(holdings.HoldingsFormatError):
        holdings.read_dir("nl", tmp_path)


def test_cli_fetch_and_status_with_holdings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from registers import cli

    monkeypatch.setenv("DATACLI_REGISTERS_ROOT", str(tmp_path / "root"))
    saved = tmp_path / "saved"
    saved.mkdir()
    (saved / "bafin_gesamtexport.csv").write_bytes(DE_SNAPSHOT)
    assert cli.main(["fetch", "--holdings", "--market", "de", "--from-dir", str(saved)]) == 0
    assert not holdings.HoldingsStore(tmp_path / "root", "de").exists()
    assert cli.main(["fetch", "--holdings", "--market", "de", "--from-dir", str(saved), "--run"]) == 0
    assert holdings.HoldingsStore(tmp_path / "root", "de").read().shape[0] == 3
    assert cli.main(["status", "--holdings", "--json"]) == 0
    out = capsys.readouterr().out
    assert '"kind": "holdings"' in out and '"rows": 3' in out
    assert cli.main(["fetch", "--holdings", "--market", "uk"]) == 2
    assert cli.main(["fetch", "--from-dir", str(saved)]) == 2


def test_parse_capital_and_issuer_kvk() -> None:
    text = (
        "Datum meldingsplicht;Uitgevende onderneming;Inschrijving handelsregister;Plaats;Totaal geplaatst kapitaal;Totaal aantal stemmen;Aantal gecertificeerd\n"
        "2026-09-30 00:00:00;Shell plc;34179503;Londen;401064827.10000;5729497530.00000;0.00000\n"
        "2026-09-30 00:00:00;Shell plc;34179503;Londen;401064827.10000;5729497530.00000;0.00000\n"
        "2026-09-30 00:00:00;CM.com N.V.;;Breda;1993315.38000;33221923.00000;0.00000\n"
        "2020-01-02 00:00:00;Nedap N.V.;08013836;Groenlo;669292.00000;6692920.00000;244114.00000\n"
    ).encode("latin-1")
    cap = holdings.parse_capital(text)
    assert list(cap.columns) == list(holdings.CAPITAL_COLUMNS) and len(cap) == 3  # the duplicate dropped
    shell = cap[cap["issuer"] == "Shell plc"].iloc[0]
    assert shell["date"] == dt.date(2026, 9, 30) and shell["issued_capital"] == pytest.approx(401064827.1) and shell["votes"] == 5729497530.0
    assert holdings.issuer_kvk(cap) == {"Shell plc": "34179503", "Nedap N.V.": "08013836"}
    with pytest.raises(holdings.HoldingsFormatError):
        holdings.parse_capital(b"x;y\n1;2\n")
