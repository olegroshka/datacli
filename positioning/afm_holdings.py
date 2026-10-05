"""DD-006 C2: the AFM holdings register's issuers mapped to ISINs and to a priced series, and its holders classified.

The AFM's substantial-holdings export names issuers only (one current name
per issuer across the whole history) and gives no ISIN. The map is built in
this order, the first source that answers wins:

1. :data:`HAND_TABLE`: issuers whose identity was verified by hand on
   2026-10-05 (renamed small caps, issuers listed abroad, the vendor's own
   naming errors); each entry names the ISIN where known and the lane and
   code that price it.
2. The AFM's **short** register (``short_register``, market ``nl``), whose
   issuer names come from the same regulator: an exact match on the
   normalised name gives every ISIN the issuer has carried (reverse splits
   and re-domiciliations change the ISIN; the price series under the
   vendor code spans them).
3. The price lanes' ticker lists, an exact match on the normalised name.

:func:`normalise_name` strips legal forms (N.V., B.V., S.A., SE, plc,
Ltd, Inc, AG, Koninklijke/Royal), punctuation and diacritics and lower-cases;
it keeps distinguishing words (``Holding``, ``Group``), so that Heineken
Holding N.V. and Heineken N.V. stay apart.

Pricing: every ``(lane, code)`` the lanes' ticker lists hold for the
issuer's ISINs is a candidate; the chosen series is the first in
:data:`LANE_PREFERENCE` that has prices on disk (the AFM's own market first,
then the other European lanes, the US lanes last), else the first candidate
without prices (``priced`` False). The hand table may name the series
directly.

Holders are classified by :func:`classify_holders`: ``hedge_fund`` when
the name matches a Form ADV adviser that runs hedge funds (exact normalised
name, else the name's core after fillers are removed) and is not a bank, a
broker, a passive or long-only house (:data:`BANK_OR_PASSIVE`, a hand
regex); ``bank_or_passive``; ``issuer`` (a holder that is itself an issuer
on the register: buy-backs and cross-holdings); ``person`` (initials and a
surname); ``other``. The rule is the one EVAL-009a pre-registers.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Mapping

import pandas as pd

MARKET = "nl"
#: The lanes whose series may price a register issuer, in order of preference.
LANE_PREFERENCE: tuple[str, ...] = (
    "nl_domestic",
    "fr_domestic",
    "de_domestic",
    "uk_domestic",
    "uk_eu",
    "no_domestic",
    "se_domestic",
    "ie_domestic",
    "us_common",
    "us_extended",
)
_LEGAL = re.compile(r"\b(nv|bv|sa|se|plc|ltd|limited|inc|incorporated|ag|koninklijke|royal)\b")
_DOTTED = re.compile(r"\b(?:[a-z]\.\s?)+(?:[a-z]\b\.?)?")
_PERSON = re.compile(r"^(?:[A-Z]{1,3}\.\s*)+(?:[a-z]+\s+)*[A-Z][\w'-]+(?:\s[\w'-]+)?$")


def normalise_name(name: str) -> str:
    """Lower-case ASCII, legal forms and punctuation removed, single spaces."""
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = _DOTTED.sub(lambda m: m.group(0).replace(".", "").replace(" ", "") + " ", s)  # n.v. -> nv, n.v -> nv, s.p.a. -> spa
    s = s.replace("&", " and ").replace("-", " ")
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _LEGAL.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


@dataclass(frozen=True)
class HandEntry:
    """A verified identity: the ISIN (None when the lists carry none), the lane and code that price it, the fact."""

    isin: str | None
    lane: str | None
    ticker: str | None
    note: str


#: Issuers verified by hand (2026-10-05): renamed, listed abroad, or misnamed by the vendor. ``lane``/``ticker`` None: not priced on any lane.
HAND_TABLE: dict[str, HandEntry] = {
    "Redcare Pharmacy N.V.": HandEntry("NL0012044747", "de_domestic", "RDC", "Frankfurt listing (formerly Shop Apotheke Europe)"),
    "Stellantis N.V.": HandEntry("NL00150001Q9", "uk_eu", "8TI", "Frankfurt listing in EUR; the vendor's series runs through Fiat Chrysler (NL0010877643)"),
    "Qiagen N.V.": HandEntry("NL0015002CX3", "de_domestic", "QIA", "XETRA listing; ISIN changed from NL0012169213 (the short register's) in 2024"),
    "argenx SE": HandEntry("NL0010832176", "us_extended", "ARGX", "Euronext Brussels, no lane; the US ADR in USD is the only series on disk"),
    "Ferrari N.V.": HandEntry("NL0011585146", "us_extended", "RACE", "Milan and New York; the NYSE series in USD is the only one on disk"),
    "Iveco Group N.V.": HandEntry(None, None, None, "Milan listing; no lane carries it"),
    "Pepco Group N.V.": HandEntry(None, None, None, "Warsaw listing; no lane carries it"),
    "Global InterConnection Group Limited": HandEntry(None, None, None, "Euronext Amsterdam CABLE since 2023-07 (ex Disruptive Capital Acquisition SPAC), renamed 2026 Corporate Holdings; not on the vendor's lists"),
    "ER Capital N.V.": HandEntry("NL0010389508", "nl_domestic", "TITAN", "Titan N.V. renamed ER Capital after the 2025 reverse listing; the vendor's TITAN code carries no ISIN"),
    "Hawick Data N.V.": HandEntry("NL0010556726", "nl_domestic", "HWK", "formerly IEX Group; the vendor labels HWK 'Green Earth Group N.V.' by mistake"),
    "Green Earth Group N.V.": HandEntry("NL0009169515", "nl_domestic", "EARTH", "formerly DGB Group"),
    "PB Holding N.V.": HandEntry("NL0000336303", "nl_domestic", "PBH", "formerly Stern Groep, renamed 2022-03-16"),
    "Lavide Holding N.V.": HandEntry("NL0010545679", "nl_domestic", "LVIDE", "vendor name 'Lavide Holding NV'"),
    "MKB NedSense N.V.": HandEntry("NL0009312842", "nl_domestic", "NEDSE", "vendor name 'NedSense Enterprises NV'"),
    "Nedap N.V.": HandEntry("NL0000371243", "nl_domestic", "NEDAP", "vendor name 'NV Nederlandsche Apparatenfabriek Nedap'"),
    "Holland Colours N.V.": HandEntry("NL0000440311", "nl_domestic", "HOLCO", ""),
    "ACOMO N.V.": HandEntry("NL0000313286", "nl_domestic", "ACOMO", "vendor name 'Amsterdam Commodities NV'"),
    "Hydratec Industries N.V.": HandEntry("NL0009391242", "nl_domestic", "HYDRA", ""),
    "N.V. Bever Holding": HandEntry("NL0000285278", "nl_domestic", "BEVER", ""),
    "N.V. Koninklijke Porceleyne Fles": HandEntry("NL0000378669", "nl_domestic", "PORF", "vendor name 'B.V. Delftsch Aardewerkfabriek \"De Porceleyne Fles Anno 1653\"'"),
    "Almunda Professionals N.V.": HandEntry(None, "nl_domestic", "AMUND", "formerly Novisource (NOVI, NL0010696704); the vendor's AMUND code carries no ISIN"),
    "Banijay Group N.V.": HandEntry("NL0015000X07", "nl_domestic", "BNJ", ""),
    "AFC Ajax N.V.": HandEntry("NL0000018034", "nl_domestic", "AJAX", ""),
    "New Amsterdam Invest N.V.": HandEntry("NL0015000CG2", "nl_domestic", "NAI", ""),
    "Triodos Bank N.V.": HandEntry(None, "nl_domestic", "TRIO", "depositary receipts; the vendor's code carries no ISIN"),
    "SWI Capital Holding Limited": HandEntry(None, "nl_domestic", "SWICH", "the vendor's code carries no ISIN"),
    "Cabka N.V.": HandEntry(None, "nl_domestic", "CABKA", "the vendor's code carries no ISIN"),
    "Morefield Group N.V.": HandEntry(None, "nl_domestic", "MORE", "formerly HeadFirst Source Group; the vendor's code carries no ISIN"),
    "Plaza Centers N.V.": HandEntry("NL0011882741", "uk_domestic", "PLAZ", "London listing in pence"),
    "Photon Energy N.V.": HandEntry(None, "de_domestic", "P7V", "XETRA listing; the vendor's code carries no ISIN"),
    "Ariston Holding N.V.": HandEntry("NL0015000N33", None, None, "Milan listing; no priced lane"),
    "Davide Campari - Milano N.V.": HandEntry("NL0015435975", None, None, "Milan listing; no priced lane"),
    "Cementir Holding N.V.": HandEntry("NL0013995087", None, None, "Milan listing; no priced lane"),
    "Brembo N.V.": HandEntry(None, None, None, "Milan listing; no lane carries it"),
    "Digi Communications N.V.": HandEntry(None, None, None, "Bucharest listing; no lane carries it"),
    "Onward Medical N.V.": HandEntry(None, None, None, "Euronext Brussels; no lane carries it"),
    "MFE-MEDIAFOREUROPE N.V.": HandEntry("NL0015000H23", None, None, "Milan listing (A and B shares NL0015001OI1, NL0015001OJ9 in the short register); no priced lane"),
    "Tetragon Financial Group Limited": HandEntry("GG00B1RMC548", "uk_eu", "TFG", "Euronext Amsterdam in USD (outside the lane's EUR filter)"),
    "Eurocastle Investment Limited": HandEntry("GB00B94QM994", "nl_domestic", "ECT", ""),
    "Technip Energies N.V.": HandEntry("NL0014559478", "fr_domestic", "TE", "Paris listing"),
    "Airbus SE": HandEntry("NL0000235190", "fr_domestic", "AIR", "Paris listing"),
    "Pluxee N.V.": HandEntry("NL0015001W49", "fr_domestic", "PLX", "Paris listing"),
    "Euronext N.V.": HandEntry("NL0006294274", "fr_domestic", "ENX", "the Amsterdam series on the lane ends 2020-01; the Paris series is complete"),
    "Ad Pepper Media International N.V.": HandEntry("NL0000238145", "uk_eu", "APM", "XETRA listing"),
    "RHI Magnesita N.V.": HandEntry("NL0012650360", "uk_eu", "RHIM", "Vienna listing in EUR (London in pence beside it)"),
    "Unilever Plc": HandEntry("GB00BVZK7T90", "uk_domestic", "ULVR", "London listing in pence; the Amsterdam line (UNA) is unpriced"),
    "RELX Plc": HandEntry("GB00B2B0DG97", "uk_domestic", "REL", "London listing in pence; the Amsterdam line (REN) is unpriced"),
    "Coca-Cola Europacific Partners plc": HandEntry("GB00BDCPN049", "uk_eu", "CCEP", "Amsterdam listing in EUR on the uk_eu lane"),
    "Volta Finance Limited": HandEntry("GG00B1GHHH78", "uk_eu", "VTA", "Amsterdam listing in EUR on the uk_eu lane"),
    "HAL Trust": HandEntry("BMG455841020", "uk_eu", "HAL", "Amsterdam listing in EUR on the uk_eu lane"),
    "VEON Ltd.": HandEntry("US91822M5022", "us_common", "VEON", "the Amsterdam line is unpriced; the Nasdaq ADR in USD"),
    "EXOR N.V.": HandEntry("NL0012059018", "uk_eu", "EXO", "Amsterdam listing since 2022-08 (Milan before); the series starts there"),
    "AEGON Ltd.": HandEntry("BMG0112X1056", "nl_domestic", "AGN", "re-domiciled to Bermuda in 2023 (NL0000303709 before)"),
    "Heineken Holding N.V.": HandEntry("NL0000008977", "nl_domestic", "HEIO", ""),
    "Alumexx N.V.": HandEntry("NL0012194724", "nl_domestic", "ALX", "formerly Phelix/Inverko (NL0011495189 in the short register)"),
    "Value8 N.V.": HandEntry("NL0010661864", "nl_domestic", "VALUE", "the ordinary share; PREVA is the preference share"),
    "BM3EAC Corp.": HandEntry(None, None, None, "Brigade-M3 European Acquisition SPAC, Euronext Amsterdam 2021-12 to 2023-09; not on the vendor's lists"),
    "Agility Capital Holding Inc.": HandEntry(None, None, None, "not on the vendor's lists"),
    "PPLA Participations Ltd.": HandEntry(None, None, None, "not on the vendor's lists"),
    "Arcona Property Fund N.V.": HandEntry(None, None, None, "not on the vendor's lists"),
    "Palmboomen Cultuur Maatschappij Mopoli (Palmeraies De Mopoli) N.V.": HandEntry(None, None, None, "not on the vendor's lists"),
}

MAP_COLUMNS: tuple[str, ...] = (
    "issuer",
    "issuer_kvk",
    "isin",
    "isins",
    "lane",
    "ticker",
    "priced",
    "priced_from",
    "priced_to",
    "source",
    "note",
)


def _lane_rank(lane: str) -> int:
    return LANE_PREFERENCE.index(lane) if lane in LANE_PREFERENCE else len(LANE_PREFERENCE)


def _candidates(isins: Iterable[str], tickers: pd.DataFrame) -> pd.DataFrame:
    wanted = {i.upper() for i in isins if i}
    if not wanted:
        return tickers.iloc[0:0]
    hits = tickers[tickers["isin"].isin(wanted)].copy()
    hits["_rank"] = hits["lane"].map(_lane_rank)
    return hits.sort_values(["_rank", "lane", "ticker"], kind="stable")


def _choose(cands: pd.DataFrame, priced: Mapping[tuple[str, str], tuple[str, str]]) -> tuple[str | None, str | None]:
    for row in cands.itertuples(index=False):
        if (row.lane, row.ticker) in priced:
            return row.lane, row.ticker
    if len(cands):
        first = cands.iloc[0]
        return str(first["lane"]), str(first["ticker"])
    return None, None


def build_issuer_map(
    issuers: Iterable[str],
    *,
    short_register: pd.DataFrame,
    tickers: pd.DataFrame,
    priced: Mapping[tuple[str, str], tuple[str, str]],
    kvk: Mapping[str, str] | None = None,
    hand_table: Mapping[str, HandEntry] | None = None,
) -> pd.DataFrame:
    """One row per issuer (:data:`MAP_COLUMNS`).

    ``short_register``: the AFM short register's rows (``issuer``, ``isin``);
    ``tickers``: the lanes' ticker lists as ``lane, ticker, name, isin``;
    ``priced``: ``(lane, ticker)`` to ``(first date, last date)`` for the
    series on disk; ``kvk``: issuer name to Chamber of Commerce number.
    """
    hand = HAND_TABLE if hand_table is None else hand_table
    kvk = kvk or {}
    tick = tickers.copy()
    tick["isin"] = tick["isin"].astype(str).str.upper().where(tick["isin"].notna(), "")
    tick["_norm"] = tick["name"].astype(str).map(normalise_name)
    short = short_register[["issuer", "isin"]].dropna().copy()
    short["_norm"] = short["issuer"].map(normalise_name)
    short["isin"] = short["isin"].str.upper()
    short_by_norm = short.groupby("_norm")["isin"].agg(lambda s: sorted(set(s)))
    rows = []
    for issuer in sorted(set(issuers)):
        norm = normalise_name(issuer)
        isins: list[str] = []
        lane = ticker = None
        source = "none"
        note = ""
        entry = hand.get(issuer)
        if entry is not None:
            source = "hand"
            note = entry.note
            if entry.isin:
                isins.append(entry.isin.upper())
            lane, ticker = entry.lane, entry.ticker
        if norm in short_by_norm.index:
            for isin in short_by_norm[norm]:
                if isin not in isins:
                    isins.append(isin)
            if source == "none":
                source = "short_register"
        by_name = tick[(tick["_norm"] == norm) & (tick["isin"] != "")]
        for isin in by_name["isin"]:
            if isin not in isins:
                isins.append(isin)
        if source == "none" and len(by_name):
            source = "lane_name"
        if lane is None and ticker is None:
            cands = _candidates(isins, tick)
            if cands.empty and source == "none":
                by_code = tick[tick["_norm"] == norm]
                if len(by_code):
                    cands = by_code.assign(_rank=by_code["lane"].map(_lane_rank)).sort_values(["_rank", "lane"], kind="stable")
                    source = "lane_name"
            lane, ticker = _choose(cands, priced)
        span = priced.get((lane, ticker)) if lane and ticker else None
        rows.append(
            dict(
                issuer=issuer,
                issuer_kvk=kvk.get(issuer, ""),
                isin=isins[0] if isins else None,
                isins="|".join(isins),
                lane=lane,
                ticker=ticker,
                priced=span is not None,
                priced_from=span[0] if span else None,
                priced_to=span[1] if span else None,
                source=source,
                note=note,
            )
        )
    return pd.DataFrame(rows, columns=list(MAP_COLUMNS))


# --------------------------------------------------------------------------- #
# visibility (DD-006 C3): the rule lives with the capture; re-exported here
# --------------------------------------------------------------------------- #
from registers.holdings import VISIBILITY_WEEKDAYS, visible_from  # noqa: E402,F401


# --------------------------------------------------------------------------- #
# holders
# --------------------------------------------------------------------------- #
_FILLERS = re.compile(
    r"\b(inc|incorporated|llc|l l c|llp|l l p|lp|l p|ltd|limited|plc|corp|corporation|co|company|sa|s a|se|ag|nv|n v|bv|b v|gmbh|the|"
    r"group|holdings|holding|management|asset|investment|investments|advisors|advisers|partners|capital|fund|funds|international|"
    r"europe|european|uk|us|usa|america|global|jersey|cayman|gp|general partner)\b"
)
#: Banks, brokers, passive and long-only houses, insurers and pension funds: excluded from the hedge-fund class however they match Form ADV.
BANK_OR_PASSIVE = re.compile(
    r"\b(bank|banque|banca|banco|bancorp|goldman|morgan|barclays|ubs|citigroup|hsbc|bnp|credit suisse|deutsche|societe generale|"
    r"natixis|nomura|macquarie|jefferies|lazard|rbc|royal bank|scotia|mizuho|sumitomo|wells fargo|state street|northern trust|mellon|bny|"
    r"blackrock|vanguard|amundi|dws|invesco|schroders?|fidelity|fmr|fil|capital research|capital group|t rowe|franklin|templeton|wellington|"
    r"janus|henderson|jupiter|legal and general|legal general|norges|apg|pggm|allianz|axa|aviva|dimensional|massachusetts financial|mfs|"
    r"ameriprise|columbia|baillie|aberdeen|abrdn|nn group|ing|aegon|asr|van lanschot|kempen|robeco|delta lloyd|achmea|generali|zurich|"
    r"prudential|manulife|sun life|principal|nuveen|natwest|lloyds|standard life|pictet|lombard|julius|vontobel|credit agricole|caixa|"
    r"santander|bbva|intesa|unicredit|commerzbank|kbc|danske|nordea|seb|swedbank|handelsbanken|dnb|rabobank|triodos|teslin|"
    r"smallcap world|europacific|growth fund|new perspective|american funds|price international|harris associates|artisan|acadian|"
    r"arrowstreet|lsv|jo hambro|j o hambro|ninety one|threadneedle|montanaro|kiltearn|silchester|mondrian|causeway|pzena|boston partners|"
    r"brandes|dodge|oakmark|wcm|polen|fisher|parametric|geode|dreyfus|pimco|alliancebernstein|bernstein|loomis|neuberger|federated|"
    r"first eagle|tweedy|eaton vance|calamos|royce|wasatch|cohen steers|stichting|pensioen|pension|ontario|cpp|omers|caisse|gic|temasek|"
    r"abu dhabi|qatar|kuwait|saudi|public investment|lingotto|exor|select equity|generation investment|jennison|southeastern|"
    r"platinum investment|third avenue|driehaus|kabouter|davis investments|compass asset|arlington asset|advent international|"
    r"tetragon|polygon)\b"
)


def _key(name: str) -> str:
    s = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode().lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _core(name: str) -> str:
    return re.sub(r"\s+", " ", _FILLERS.sub(" ", _key(name))).strip()


HOLDER_COLUMNS: tuple[str, ...] = ("holder", "kind", "adv_match")
KINDS: tuple[str, ...] = ("hedge_fund", "bank_or_passive", "issuer", "person", "other")
MIN_CORE_LENGTH = 4


def classify_holders(holders: Iterable[str], *, adv_hedge_fund_names: Iterable[str], issuers: Iterable[str] = ()) -> pd.DataFrame:
    """One row per holder with its ``kind`` (:data:`KINDS`) and how it matched Form ADV (``exact``, ``core`` or empty).

    ``adv_hedge_fund_names``: the names (business and legal) of the advisers
    whose Form ADV reports hedge funds; ``issuers``: the register's issuer
    names (a holder equal to one is the issuer itself).
    """
    exact_keys: set[str] = set()
    core_keys: set[str] = set()
    for name in adv_hedge_fund_names:
        if not name or pd.isna(name):
            continue
        exact_keys.add(_key(name))
        core = _core(name)
        if len(core) >= MIN_CORE_LENGTH:
            core_keys.add(core)
    issuer_keys = {_key(i) for i in issuers}
    rows = []
    for holder in sorted(set(holders)):
        key = _key(holder)
        core = _core(holder)
        match = "exact" if key in exact_keys else ("core" if len(core) >= MIN_CORE_LENGTH and core in core_keys else "")
        if key in issuer_keys:
            kind = "issuer"
        elif BANK_OR_PASSIVE.search(key):
            kind = "bank_or_passive"
        elif match:
            kind = "hedge_fund"
        elif _PERSON.match(holder.strip()):
            kind = "person"
        else:
            kind = "other"
        rows.append(dict(holder=holder, kind=kind, adv_match=match))
    return pd.DataFrame(rows, columns=list(HOLDER_COLUMNS))


# --------------------------------------------------------------------------- #
# coverage (the C2 check)
# --------------------------------------------------------------------------- #
COVERAGE_COLUMNS: tuple[str, ...] = ("subset", "notifications", "issuers", "with_isin", "with_ticker", "priced_on_disk", "priced_at_date")


def coverage(notifications: pd.DataFrame, issuer_map: pd.DataFrame, holders: pd.DataFrame | None = None) -> pd.DataFrame:
    """Shares of notifications whose issuer has an ISIN, a ticker, a price series on disk, and one covering the obligation date.

    One row for every notification and, when ``holders`` is given, one per
    holder kind.
    """
    m = issuer_map.set_index("issuer")
    work = notifications[["obligation_date", "issuer", "holder"]].copy()
    work["_isin"] = work["issuer"].map(m["isin"]).notna()
    work["_ticker"] = work["issuer"].map(m["ticker"]).notna()
    work["_priced"] = work["issuer"].map(m["priced"]).fillna(False).astype(bool)
    first = pd.to_datetime(work["issuer"].map(m["priced_from"]), errors="coerce")
    last = pd.to_datetime(work["issuer"].map(m["priced_to"]), errors="coerce")
    date = pd.to_datetime(work["obligation_date"])
    work["_at_date"] = work["_priced"] & (first <= date) & (date <= last + pd.Timedelta(days=7))
    if holders is not None:
        work["kind"] = work["holder"].map(holders.set_index("holder")["kind"]).fillna("other")
    rows = [_coverage_row("all", work)]
    if holders is not None:
        for kind in KINDS:
            sub = work[work["kind"] == kind]
            if len(sub):
                rows.append(_coverage_row(kind, sub))
    return pd.DataFrame(rows, columns=list(COVERAGE_COLUMNS))


def _coverage_row(label: str, work: pd.DataFrame) -> dict[str, object]:
    n = len(work)
    return dict(
        subset=label,
        notifications=n,
        issuers=int(work["issuer"].nunique()),
        with_isin=round(float(work["_isin"].mean()), 3) if n else float("nan"),
        with_ticker=round(float(work["_ticker"].mean()), 3) if n else float("nan"),
        priced_on_disk=round(float(work["_priced"].mean()), 3) if n else float("nan"),
        priced_at_date=round(float(work["_at_date"].mean()), 3) if n else float("nan"),
    )
