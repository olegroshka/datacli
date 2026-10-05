"""registers.holdings: the AFM substantial-holdings export and the issued-capital register on small fixtures."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

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
