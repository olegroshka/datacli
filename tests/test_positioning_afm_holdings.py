"""positioning.afm_holdings: issuer normalisation, the ISIN map, holder classification and the coverage check on synthetic data."""

from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import afm_holdings as ah  # noqa: E402


def test_normalise_name_strips_legal_forms_and_keeps_distinguishing_words() -> None:
    assert ah.normalise_name("Koninklijke Ahold Delhaize N.V.") == "ahold delhaize"
    assert ah.normalise_name("Koninklijke Ahold Delhaize NV") == "ahold delhaize"
    assert ah.normalise_name("Heineken Holding N.V.") == "heineken holding"
    assert ah.normalise_name("Heineken N.V.") == "heineken"
    assert ah.normalise_name("Société Générale S.A.") == "societe generale"
    assert ah.normalise_name("Basic-Fit N.V.") == ah.normalise_name("Basic Fit NV")
    assert ah.normalise_name("N.V. Bever Holding") == "bever holding"
    assert ah.normalise_name("Allfunds Group plc") == "allfunds group"
    assert ah.normalise_name("Ctac N.V") == "ctac"  # the vendor drops the last dot
    assert ah.normalise_name("Davide Campari - Milano S.p.A.") == "davide campari milano spa"


def _tickers() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("nl_domestic", "AALB", "Aalberts Industries NV", "NL0000852564"),
            ("uk_eu", "AALB", "Aalberts Industries NV", "NL0000852564"),
            ("nl_domestic", "FUR", "Fugro N.V.", "NL00150003E1"),
            ("nl_domestic", "HEIA", "Heineken", "NL0000009165"),
            ("nl_domestic", "HEIO", "Heineken Holding NV", "NL0000008977"),
            ("nl_domestic", "HWK", "Green Earth Group N.V.", "NL0010556726"),  # the vendor's wrong label
            ("nl_domestic", "EARTH", "GREEN EARTH GROUP N.V.", "NL0009169515"),
            ("fr_domestic", "STMPA", "STMicroelectronics NV", "NL0000226223"),
            ("nl_domestic", "CABKA", "Cabka N.V.", None),
            ("nl_domestic", "BFIT", "Basic Fit NV", "NL0011872650"),
        ],
        columns=["lane", "ticker", "name", "isin"],
    )


def _short() -> pd.DataFrame:
    return pd.DataFrame(
        [
            ("Aalberts N.V.", "NL0000852564"),
            ("Fugro N.V.", "NL0000352565"),
            ("Fugro N.V.", "NL00150003E1"),
            ("STMicroelectronics N.V.", "NL0000226223"),
            ("Heineken N.V.", "NL0000009165"),
        ],
        columns=["issuer", "isin"],
    )


PRICED = {
    ("nl_domestic", "AALB"): ("2012-01-02", "2026-10-02"),
    ("nl_domestic", "FUR"): ("2012-01-02", "2026-10-02"),
    ("nl_domestic", "HEIA"): ("2012-01-02", "2026-10-02"),
    ("fr_domestic", "STMPA"): ("2012-01-02", "2026-10-02"),
    ("uk_eu", "AALB"): ("2005-01-03", "2026-10-02"),
}


def test_build_issuer_map_prefers_hand_then_short_register_then_lane_names_and_prices_cross_lane() -> None:
    issuers = ["Aalberts N.V.", "Fugro N.V.", "STMicroelectronics N.V.", "Heineken Holding N.V.", "Heineken N.V.", "Hawick Data N.V.",
               "Green Earth Group N.V.", "Cabka N.V.", "Basic-Fit N.V.", "Nobody N.V."]
    hand = {
        "Hawick Data N.V.": ah.HandEntry("NL0010556726", "nl_domestic", "HWK", "formerly IEX Group"),
        "Green Earth Group N.V.": ah.HandEntry("NL0009169515", "nl_domestic", "EARTH", "formerly DGB"),
        "Cabka N.V.": ah.HandEntry(None, "nl_domestic", "CABKA", "no ISIN on the list"),
    }
    out = ah.build_issuer_map(issuers, short_register=_short(), tickers=_tickers(), priced=PRICED, kvk={"Fugro N.V.": "27120091"}, hand_table=hand).set_index("issuer")
    assert list(out.columns) == [c for c in ah.MAP_COLUMNS if c != "issuer"]
    aalb = out.loc["Aalberts N.V."]
    assert aalb["source"] == "short_register" and aalb["isin"] == "NL0000852564" and (aalb["lane"], aalb["ticker"]) == ("nl_domestic", "AALB")
    assert bool(aalb["priced"]) is True and aalb["priced_from"] == "2012-01-02"
    fugro = out.loc["Fugro N.V."]
    assert fugro["isins"] == "NL0000352565|NL00150003E1" and fugro["ticker"] == "FUR" and fugro["issuer_kvk"] == "27120091"
    stm = out.loc["STMicroelectronics N.V."]
    assert (stm["lane"], stm["ticker"]) == ("fr_domestic", "STMPA") and bool(stm["priced"]) is True  # no NL series: the Paris one
    # Heineken Holding is not Heineken: the lane list by name, unpriced
    hh = out.loc["Heineken Holding N.V."]
    assert hh["source"] == "lane_name" and hh["isin"] == "NL0000008977" and hh["ticker"] == "HEIO" and bool(hh["priced"]) is False
    assert out.loc["Heineken N.V.", "ticker"] == "HEIA"
    # the hand table overrides the vendor's wrong label on HWK
    assert out.loc["Hawick Data N.V.", "ticker"] == "HWK" and out.loc["Hawick Data N.V.", "source"] == "hand"
    assert out.loc["Green Earth Group N.V.", "ticker"] == "EARTH" and out.loc["Green Earth Group N.V.", "isin"] == "NL0009169515"
    cabka = out.loc["Cabka N.V."]
    assert pd.isna(cabka["isin"]) and cabka["ticker"] == "CABKA" and bool(cabka["priced"]) is False
    assert out.loc["Basic-Fit N.V.", "ticker"] == "BFIT"  # hyphen against space
    nobody = out.loc["Nobody N.V."]
    assert nobody["source"] == "none" and pd.isna(nobody["isin"]) and pd.isna(nobody["ticker"]) and bool(nobody["priced"]) is False


def test_visible_from_adds_two_weekdays_over_weekends() -> None:
    dates = pd.Series([dt.date(2026, 10, 1), dt.date(2026, 10, 2), dt.date(2026, 10, 3), dt.date(2026, 10, 5)])  # Thu, Fri, Sat, Mon
    out = ah.visible_from(dates)
    assert out.tolist() == [dt.date(2026, 10, 5), dt.date(2026, 10, 6), dt.date(2026, 10, 6), dt.date(2026, 10, 7)]
    assert ah.visible_from(dates, weekdays=1).tolist()[0] == dt.date(2026, 10, 2)


def test_classify_holders_matches_adv_excludes_banks_and_recognises_issuers_and_persons() -> None:
    adv = ["Citadel Advisors LLC", "MILLENNIUM MANAGEMENT LLC", "Goldman Sachs Asset Management, L.P.", "Weiss Asset Management LP", "RTW Investments, LP", "ABC"]
    holders = ["Citadel Advisors LLC", "Millennium International Management LP", "Goldman Sachs Group Inc., The", "Weiss Asset Management LP",
               "RTW Investments, LP", "P.P.F. de Vries", "J.P.  Visser", "Signify N.V.", "Stichting Continuiteit TomTom", "Teslin Participaties Coöperatief U.A.", "Unknown Partners LLP", "ABC Capital"]
    out = ah.classify_holders(holders, adv_hedge_fund_names=adv, issuers=["Signify N.V."]).set_index("holder")
    assert list(out.columns) == ["kind", "adv_match"]
    assert out.loc["Citadel Advisors LLC"].tolist() == ["hedge_fund", "exact"]
    assert out.loc["Millennium International Management LP"].tolist() == ["hedge_fund", "core"]  # 'millennium' after fillers
    assert out.loc["Goldman Sachs Group Inc., The", "kind"] == "bank_or_passive"
    assert out.loc["Weiss Asset Management LP", "kind"] == "hedge_fund" and out.loc["RTW Investments, LP", "kind"] == "hedge_fund"
    assert out.loc["P.P.F. de Vries", "kind"] == "person" and out.loc["J.P.  Visser", "kind"] == "person"
    assert out.loc["Signify N.V.", "kind"] == "issuer"
    assert out.loc["Stichting Continuiteit TomTom", "kind"] == "bank_or_passive"
    assert out.loc["Teslin Participaties Coöperatief U.A.", "kind"] == "bank_or_passive"
    assert out.loc["Unknown Partners LLP", "kind"] == "other"
    assert out.loc["ABC Capital", "kind"] == "other"  # a core shorter than four characters never matches


def test_coverage_counts_isin_ticker_and_priced_shares_per_holder_kind() -> None:
    notes = pd.DataFrame(
        {
            "obligation_date": [dt.date(2020, 1, 10), dt.date(2024, 5, 1), dt.date(2010, 3, 3), dt.date(2024, 5, 1)],
            "issuer": ["A", "A", "B", "C"],
            "holder": ["HF1", "Bank1", "HF1", "HF2"],
        }
    )
    issuer_map = pd.DataFrame(
        {
            "issuer": ["A", "B", "C"],
            "isin": ["NL1", "NL2", None],
            "ticker": ["AAA", None, None],
            "priced": [True, False, False],
            "priced_from": ["2012-01-02", None, None],
            "priced_to": ["2026-10-02", None, None],
        }
    )
    holders = pd.DataFrame({"holder": ["HF1", "HF2", "Bank1"], "kind": ["hedge_fund", "hedge_fund", "bank_or_passive"], "adv_match": ["exact", "exact", ""]})
    cov = ah.coverage(notes, issuer_map, holders).set_index("subset")
    assert list(cov.columns) == [c for c in ah.COVERAGE_COLUMNS if c != "subset"]
    assert cov.loc["all", "notifications"] == 4 and cov.loc["all", "issuers"] == 3
    assert cov.loc["all", "with_isin"] == 0.75 and cov.loc["all", "with_ticker"] == 0.5 and cov.loc["all", "priced_on_disk"] == 0.5
    assert cov.loc["all", "priced_at_date"] == 0.5
    hf = cov.loc["hedge_fund"]
    assert hf["notifications"] == 3 and hf["with_isin"] == pytest_round(2 / 3) and hf["priced_on_disk"] == pytest_round(1 / 3)
    assert cov.loc["bank_or_passive", "priced_at_date"] == 1.0
    assert "person" not in cov.index


def pytest_round(x: float) -> float:
    return round(x, 3)
