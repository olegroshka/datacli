"""The AFM's substantial-holdings register (the long-side register, DD-006): the file parse.

The AFM publishes the whole register of major-holdings notifications (3
percent of capital or votes, the Transparency Directive) as one CSV export,
semicolon-separated, Latin-1, two lines per instrument line of a
notification: one carrying the notification's totals in percent of
**capital** (``Kapitaalbelang``), one in percent of **votes**
(``Stemrecht``). The totals (the total holding and its four components:
direct and indirect, real and potential) are notification-level and
repeated on every line; the line-level content is the instrument, whether
it is a real or a potential interest, direct or indirect, and its share and
vote counts. The file carries the date the obligation to notify arose, not
the publication date (DD-006 C3 fixes the visibility rule), and names the
issuer without an ISIN (DD-006 C2, :mod:`positioning.afm_holdings`, maps
the names).

:func:`parse_holdings` returns the line-level frame (:data:`LINE_COLUMNS`);
:func:`notifications` folds it to one row per ``(obligation_date, issuer,
holder)`` (:data:`NOTIFICATION_COLUMNS`). :func:`parse_capital` reads the
issued-capital register (per issuer and date, the issued capital and the
votes, with the issuer's Chamber of Commerce number).

Facts of the files are in KB-003 section 10 (probed 2026-10-05).
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

#: The export's 23 columns in order; the two ``reëel`` headers are matched by prefix (Latin-1).
HOLDINGS_HEADER_PREFIXES: tuple[str, ...] = (
    "Datum meldingsplicht",
    "Uitgevende instelling",
    "Meldingsplichtige",
    "Kvk-nr",
    "Plaats",
    "Soort aandeel",
    "Kapitaalbelang",
    "Stemrecht",
    "Wijze van beschikken",
    "Aantal aandelen",
    "Aantal stemmen",
    "Aantal equivalente aandelen",
    "Soort aandeel ENG",
    "Toelichting",
    "Soort aandeel procentuele verdeling",
    "Totale deelneming",
    "Rechtstreeks re",
    "Rechtstreeks potentieel",
    "Middellijk re",
    "Middellijk potentieel",
    "Totaal kapitaalbelang",
    "Rechtstreeks",
    "Middellijk",
)
_RAW_NAMES: tuple[str, ...] = (
    "obligation_date",
    "issuer",
    "holder",
    "holder_kvk",
    "holder_seat",
    "instrument_nl",
    "capital_kind",
    "voting_kind",
    "disposal",
    "shares",
    "votes",
    "equivalent_shares",
    "instrument",
    "document",
    "breakdown",
    "total_pct",
    "direct_real_pct",
    "direct_potential_pct",
    "indirect_real_pct",
    "indirect_potential_pct",
    "new_total_capital_pct",
    "new_direct_pct",
    "new_indirect_pct",
)
CAPITAL_BREAKDOWN = "Kapitaalbelang"
VOTING_BREAKDOWN = "Stemrecht"
#: The line-level key: everything but the breakdown kind and the percentages.
_LINE_KEY: tuple[str, ...] = _RAW_NAMES[:14]
_PCT_SUFFIXES: tuple[str, ...] = ("", "_direct_real", "_direct_potential", "_indirect_real", "_indirect_potential")

LINE_COLUMNS: tuple[str, ...] = (
    "obligation_date",
    "issuer",
    "holder",
    "holder_kvk",
    "holder_seat",
    "instrument",
    "instrument_nl",
    "real_or_potential",
    "voting_real_or_potential",
    "direct_or_indirect",
    "via",
    "shares",
    "votes",
    "equivalent_shares",
    "document",
    "pct_capital",
    "pct_capital_direct_real",
    "pct_capital_direct_potential",
    "pct_capital_indirect_real",
    "pct_capital_indirect_potential",
    "pct_voting",
    "pct_voting_direct_real",
    "pct_voting_direct_potential",
    "pct_voting_indirect_real",
    "pct_voting_indirect_potential",
)
NOTIFICATION_COLUMNS: tuple[str, ...] = (
    "obligation_date",
    "issuer",
    "holder",
    "holder_kvk",
    "holder_seat",
    "pct_capital",
    "pct_capital_real",
    "pct_capital_potential",
    "pct_capital_direct_real",
    "pct_capital_direct_potential",
    "pct_capital_indirect_real",
    "pct_capital_indirect_potential",
    "pct_voting",
    "pct_voting_real",
    "pct_voting_potential",
    "n_lines",
    "instruments",
    "has_potential",
    "shares_real",
    "shares_potential",
)
CAPITAL_HEADER: tuple[str, ...] = (
    "Datum meldingsplicht",
    "Uitgevende onderneming",
    "Inschrijving handelsregister",
    "Plaats",
    "Totaal geplaatst kapitaal",
    "Totaal aantal stemmen",
    "Aantal gecertificeerd",
)
CAPITAL_COLUMNS: tuple[str, ...] = ("date", "issuer", "issuer_kvk", "issuer_seat", "issued_capital", "votes", "certified")


class HoldingsFormatError(Exception):
    """The export does not look like the AFM's substantial-holdings file; the message is user-facing."""


@dataclass(frozen=True)
class ParsedHoldings:
    lines: pd.DataFrame
    #: Lines whose breakdown kind was neither capital nor votes (the export's stray rows).
    dropped: int
    #: Exact duplicate lines removed after the two breakdown kinds were merged.
    duplicates: int
    #: Notifications whose repeated totals disagreed between their lines (reported, the first kept).
    inconsistent_totals: int


def _read(data: bytes | str | Path) -> pd.DataFrame:
    if isinstance(data, (bytes, bytearray)):
        source: object = io.BytesIO(bytes(data))
    else:
        source = Path(data)
    try:
        return pd.read_csv(source, sep=";", encoding="latin-1", dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise HoldingsFormatError(f"not a readable semicolon CSV: {exc}") from None


def percent(series: pd.Series) -> pd.Series:
    """``3,09 %`` to 3.09; blanks and dashes to NaN."""
    text = series.astype(str).str.replace("%", "", regex=False).str.replace(",", ".", regex=False).str.strip()
    return pd.to_numeric(text.replace({"": None, "-": None}), errors="coerce")


def _number(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(",", ".", regex=False).str.strip().replace({"": None}), errors="coerce")


def _check_header(columns: list[str], expected: tuple[str, ...], *, what: str) -> None:
    if len(columns) != len(expected):
        raise HoldingsFormatError(f"{what}: expected {len(expected)} columns, got {len(columns)}: {columns}")
    for got, want in zip(columns, expected):
        if not got.startswith(want):
            raise HoldingsFormatError(f"{what}: unexpected column {got!r} where {want!r} was expected")


def parse_holdings(data: bytes | str | Path) -> ParsedHoldings:
    """The export as one row per instrument line, the capital and the voting totals side by side."""
    raw = _read(data)
    _check_header([str(c) for c in raw.columns], HOLDINGS_HEADER_PREFIXES, what="holdings export")
    raw.columns = list(_RAW_NAMES)
    kinds = raw["breakdown"].str.strip()
    capital = raw[kinds == CAPITAL_BREAKDOWN].drop(columns=["breakdown"])
    voting = raw[kinds == VOTING_BREAKDOWN].drop(columns=["breakdown"])
    dropped = int((~kinds.isin((CAPITAL_BREAKDOWN, VOTING_BREAKDOWN))).sum())
    pct_cols = list(_RAW_NAMES[15:20])
    before = len(capital)
    capital = capital.drop(columns=list(_RAW_NAMES[20:])).drop_duplicates()
    voting = voting.drop(columns=list(_RAW_NAMES[20:])).drop_duplicates()
    merged = capital.merge(voting, on=list(_LINE_KEY), how="left", suffixes=("_c", "_v"))
    merged = merged.drop_duplicates(subset=list(_LINE_KEY))
    duplicates = before - len(merged)
    out = pd.DataFrame(index=merged.index)
    date = pd.to_datetime(merged["obligation_date"].str.slice(0, 10), errors="coerce")
    if date.isna().any():
        raise HoldingsFormatError(f"{int(date.isna().sum())} lines without an obligation date")
    out["obligation_date"] = date.dt.date
    for column in ("issuer", "holder", "holder_kvk", "holder_seat", "instrument", "instrument_nl", "document"):
        out[column] = merged[column].str.strip()
    out["real_or_potential"] = _kind(merged["capital_kind"])
    out["voting_real_or_potential"] = _kind(merged["voting_kind"])
    disposal = merged["disposal"].str.split("<BR>", n=1, expand=True)
    head = disposal[0].str.strip().str.lower()
    out["direct_or_indirect"] = head.map({"rechtstreeks": "direct", "middellijk": "indirect"})
    out["via"] = disposal[1].str.strip().str.strip("()").str.strip() if disposal.shape[1] > 1 else ""
    out["via"] = out["via"].fillna("")
    for column in ("shares", "votes", "equivalent_shares"):
        out[column] = _number(merged[column])
    for raw_name, suffix in zip(pct_cols, _PCT_SUFFIXES):
        out["pct_capital" + suffix] = percent(merged[raw_name + "_c"])
        out["pct_voting" + suffix] = percent(merged[raw_name + "_v"])
    out = out[list(LINE_COLUMNS)].sort_values(["obligation_date", "issuer", "holder", "instrument"], kind="stable").reset_index(drop=True)
    inconsistent = _inconsistent_totals(out)
    return ParsedHoldings(out, dropped=dropped, duplicates=duplicates, inconsistent_totals=inconsistent)


def _kind(series: pd.Series) -> pd.Series:
    text = series.str.strip().str.lower()
    return text.map(lambda v: "potential" if v.startswith("potentieel") else ("real" if v.startswith("re") else None))


def _inconsistent_totals(lines: pd.DataFrame) -> int:
    by = lines.groupby(["obligation_date", "issuer", "holder"], sort=False)["pct_capital"].agg(["min", "max"])
    return int(((by["max"] - by["min"]).abs() > 1e-9).sum())


def notifications(lines: pd.DataFrame) -> pd.DataFrame:
    """One row per ``(obligation_date, issuer, holder)``: the totals (the first line's) and the lines' content folded."""
    if lines.empty:
        return pd.DataFrame(columns=list(NOTIFICATION_COLUMNS))
    key = ["obligation_date", "issuer", "holder"]
    work = lines.copy()
    work["_potential"] = work["real_or_potential"] == "potential"
    work["_shares_real"] = work["shares"].where(~work["_potential"], 0.0).fillna(0.0)
    work["_shares_potential"] = work["shares"].where(work["_potential"], 0.0).fillna(0.0)
    g = work.groupby(key, sort=True)
    out = g.agg(
        holder_kvk=("holder_kvk", "first"),
        holder_seat=("holder_seat", "first"),
        pct_capital=("pct_capital", "first"),
        pct_capital_direct_real=("pct_capital_direct_real", "first"),
        pct_capital_direct_potential=("pct_capital_direct_potential", "first"),
        pct_capital_indirect_real=("pct_capital_indirect_real", "first"),
        pct_capital_indirect_potential=("pct_capital_indirect_potential", "first"),
        pct_voting=("pct_voting", "first"),
        pct_voting_direct_real=("pct_voting_direct_real", "first"),
        pct_voting_direct_potential=("pct_voting_direct_potential", "first"),
        pct_voting_indirect_real=("pct_voting_indirect_real", "first"),
        pct_voting_indirect_potential=("pct_voting_indirect_potential", "first"),
        n_lines=("instrument", "size"),
        instruments=("instrument", lambda s: "|".join(sorted(set(s)))),
        has_potential=("_potential", "any"),
        shares_real=("_shares_real", "sum"),
        shares_potential=("_shares_potential", "sum"),
    ).reset_index()
    out["pct_capital_real"] = out["pct_capital_direct_real"].fillna(0.0) + out["pct_capital_indirect_real"].fillna(0.0)
    out["pct_capital_potential"] = out["pct_capital_direct_potential"].fillna(0.0) + out["pct_capital_indirect_potential"].fillna(0.0)
    out["pct_voting_real"] = out["pct_voting_direct_real"].fillna(0.0) + out["pct_voting_indirect_real"].fillna(0.0)
    out["pct_voting_potential"] = out["pct_voting_direct_potential"].fillna(0.0) + out["pct_voting_indirect_potential"].fillna(0.0)
    return out[list(NOTIFICATION_COLUMNS)].reset_index(drop=True)


def parse_capital(data: bytes | str | Path) -> pd.DataFrame:
    """The issued-capital register: per issuer and date the issued capital, the votes and the certified shares."""
    raw = _read(data)
    _check_header([str(c) for c in raw.columns], CAPITAL_HEADER, what="issued-capital export")
    raw.columns = list(CAPITAL_COLUMNS)
    out = pd.DataFrame(index=raw.index)
    date = pd.to_datetime(raw["date"].str.slice(0, 10), errors="coerce")
    out["date"] = date.dt.date
    for column in ("issuer", "issuer_kvk", "issuer_seat"):
        out[column] = raw[column].str.strip()
    for column in ("issued_capital", "votes", "certified"):
        out[column] = _number(raw[column])
    out = out[date.notna()].drop_duplicates().sort_values(["issuer", "date"], kind="stable").reset_index(drop=True)
    return out[list(CAPITAL_COLUMNS)]


def issuer_kvk(capital: pd.DataFrame) -> dict[str, str]:
    """Issuer name to its Chamber of Commerce number from the capital register (the latest non-empty one)."""
    rows = capital[capital["issuer_kvk"].astype(str).str.strip() != ""].sort_values("date")
    return dict(zip(rows["issuer"], rows["issuer_kvk"].astype(str).str.strip()))
