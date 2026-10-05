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

import datetime as dt
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow as pa

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


# --------------------------------------------------------------------------- #
# visibility (DD-006 C3)
# --------------------------------------------------------------------------- #
#: The AFM file carries the obligation date, not the publication date. The law allows four
#: trading days to notify and the AFM publishes on receipt; until the daily capture measures
#: the lag (``first_seen`` minus the obligation date), a row is taken as visible from the
#: second weekday after the obligation date (Dutch holidays ignored, recorded as a limit).
VISIBILITY_WEEKDAYS = 2


def visible_from(obligation_dates: pd.Series, weekdays: int = VISIBILITY_WEEKDAYS) -> pd.Series:
    """The first day a notification is taken as public: ``weekdays`` Monday-to-Friday days after the obligation date."""
    dates = pd.to_datetime(obligation_dates)
    out = dates + pd.offsets.BDay(weekdays)
    return pd.Series(out.dt.date.to_numpy(), index=obligation_dates.index, name="published_from")


# --------------------------------------------------------------------------- #
# BaFin (Germany): the current snapshot of voting-rights holdings
# --------------------------------------------------------------------------- #
DE_COLUMNS: tuple[str, ...] = (
    "issuer",
    "issuer_seat",
    "issuer_country",
    "holder",
    "holder_seat",
    "holder_country",
    "pct_voting",
    "pct_instruments",
    "pct_total",
    "published_at",
)


def parse_de(data: bytes | str | Path) -> pd.DataFrame:
    """BaFin's ``Gesamtexport``: UTF-8 with BOM, semicolon, no header, ten columns (KB-003 section 10)."""
    if isinstance(data, (bytes, bytearray)):
        source: object = io.BytesIO(bytes(data))
    else:
        source = Path(data)
    try:
        raw = pd.read_csv(source, sep=";", encoding="utf-8-sig", header=None, dtype=str, keep_default_na=False)
    except Exception as exc:  # noqa: BLE001
        raise HoldingsFormatError(f"not a readable semicolon CSV: {exc}") from None
    if raw.shape[1] != len(DE_COLUMNS):
        raise HoldingsFormatError(f"BaFin export: expected {len(DE_COLUMNS)} columns, got {raw.shape[1]}")
    raw.columns = list(DE_COLUMNS)
    out = raw.copy()
    for column in DE_COLUMNS[:6]:
        out[column] = out[column].str.strip()
    for column in ("pct_voting", "pct_instruments", "pct_total"):
        out[column] = _number(out[column])
    published = pd.to_datetime(out["published_at"].str.strip(), format="%d.%m.%Y", errors="coerce")
    out["published_at"] = published.dt.date.astype(object).where(published.notna(), None)
    bad = out["issuer"].eq("") | out["holder"].eq("")
    if bad.mean() > 0.02:
        raise HoldingsFormatError(f"BaFin export: {int(bad.sum())} rows without an issuer or a holder")
    return out[~bad].drop_duplicates().reset_index(drop=True)[list(DE_COLUMNS)]


# --------------------------------------------------------------------------- #
# capture (DD-006 C1): the sources, the accumulating stores, the refresh rule
# --------------------------------------------------------------------------- #
HOLDINGS_MARKETS: tuple[str, ...] = ("nl", "de")
NL_HOLDINGS_URL = "https://www.afm.nl/export.aspx?type=1331d46f-3fb6-4a36-b903-9584972675af&format=csv"
NL_CAPITAL_URL = "https://www.afm.nl/export.aspx?type=f25d2ca1-b93c-4331-b025-85df328cd505&format=csv"
DE_PORTAL_URL = "https://portal.mvp.bafin.de/database/AnteileInfo/"
DE_EXPORT_URL = "https://portal.mvp.bafin.de/database/AnteileInfo/aktiengesellschaft.do?d-3611442-e=1&cmd=zeigeGesamtExport&6578706f7274=1"
#: The saved exports a ``--from-dir`` seeding reads (the 2026-10-05 probe's names).
DIR_FILES: dict[str, dict[str, str]] = {
    "nl": {"holdings": "afm_holdings.csv", "capital": "afm_capital.csv"},
    "de": {"snapshot": "bafin_gesamtexport.csv"},
}
SUBDIR = "holdings"
NL_KEY: tuple[str, ...] = ("obligation_date", "issuer", "holder")
DE_KEY: tuple[str, ...] = ("issuer", "holder", "published_at", "pct_total")
Payload = dict[str, bytes]


def fetch_nl() -> tuple[Payload, str]:
    from registers.eu import _get  # the shared HTTP helper with the research user agent

    return {"holdings": _get(NL_HOLDINGS_URL, timeout=600.0), "capital": _get(NL_CAPITAL_URL)}, "afm holdings + capital exports"


def fetch_de() -> tuple[Payload, str]:
    """The portal page first (a session cookie), then the export."""
    import urllib.request
    from http.cookiejar import CookieJar

    from registers.eu import _get

    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    _get(DE_PORTAL_URL, opener=opener)
    return {"snapshot": _get(DE_EXPORT_URL, opener=opener)}, "bafin gesamtexport"


FETCHERS = {"nl": fetch_nl, "de": fetch_de}


def read_dir(market: str, directory: Path) -> tuple[Payload, str, dt.date]:
    """The saved exports of ``market`` in ``directory`` and the newest file's modification date."""
    payload: Payload = {}
    newest = 0.0
    for part, name in DIR_FILES[market].items():
        path = Path(directory) / name
        if not path.exists():
            raise HoldingsFormatError(f"{path} is missing")
        payload[part] = path.read_bytes()
        newest = max(newest, path.stat().st_mtime)
    return payload, f"saved exports in {directory}", dt.datetime.fromtimestamp(newest, dt.timezone.utc).date()


class HoldingsStore:
    """One market's long-register store under ``<root>/holdings/<market>/``.

    The Netherlands: ``notifications.parquet`` (the accumulated notifications
    with ``published_from``, ``first_seen`` and ``last_seen``), ``lines.parquet``
    and ``capital.parquet`` (the latest export as parsed). Germany:
    ``holdings.parquet`` (the accumulated snapshot rows with ``first_seen`` and
    ``last_seen``). Both: ``state.json``.
    """

    def __init__(self, root: Path, market: str) -> None:
        if market not in HOLDINGS_MARKETS:
            raise HoldingsFormatError(f"unknown holdings market {market!r}; expected one of {', '.join(HOLDINGS_MARKETS)}")
        self.market = market
        self.dir = Path(root) / SUBDIR / market
        self.path = self.dir / ("notifications.parquet" if market == "nl" else "holdings.parquet")
        self.state_path = self.dir / "state.json"

    def exists(self) -> bool:
        return self.path.exists()

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def read(self) -> pd.DataFrame | None:
        return pd.read_parquet(self.path) if self.path.exists() else None

    def write(self, rows: pd.DataFrame, state: dict[str, Any], extras: dict[str, pd.DataFrame] | None = None) -> None:
        import _atomic  # type: ignore[import-not-found]

        self.dir.mkdir(parents=True, exist_ok=True)
        for name, frame in {self.path.name: rows, **{f"{k}.parquet": v for k, v in (extras or {}).items()}}.items():
            _atomic.write_table(pa.Table.from_pandas(frame, preserve_index=False), self.dir / name)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        _atomic.replace_with_retry(tmp, self.state_path)


def _keys(frame: pd.DataFrame, key: tuple[str, ...]) -> pd.Index:
    parts = frame[list(key)].copy()
    for column in key:
        if column in ("obligation_date", "published_at"):
            parts[column] = pd.to_datetime(parts[column], errors="coerce").dt.strftime("%Y-%m-%d").fillna("")
        elif parts[column].dtype.kind == "f":
            parts[column] = parts[column].round(6).astype(str)
        else:
            parts[column] = parts[column].astype(str)
    return pd.MultiIndex.from_frame(parts)


def accumulate(stored: pd.DataFrame | None, fresh: pd.DataFrame, key: tuple[str, ...], seen: dt.date) -> tuple[pd.DataFrame, int, int]:
    """The stored rows (immutable once stored) plus the fresh rows not yet stored, dated ``first_seen = seen``.

    A stored row no longer in the fresh file gets ``last_seen = seen`` once
    (the regulator removed or corrected it; BaFin's holders leave the
    snapshot when they fall below the threshold). Returns ``(rows, added,
    closed)``.
    """
    fresh = fresh.copy()
    fresh["first_seen"] = seen
    fresh["last_seen"] = None
    if stored is None or stored.empty:
        return fresh.reset_index(drop=True), len(fresh), 0
    stored = stored.copy()
    stored_keys = _keys(stored, key)
    fresh_keys = _keys(fresh, key)
    new = fresh[~fresh_keys.isin(stored_keys)]
    gone = ~stored_keys.isin(fresh_keys) & stored["last_seen"].isna()
    stored.loc[gone, "last_seen"] = seen
    out = pd.concat([stored, new[list(stored.columns)]], ignore_index=True)
    return out, int(len(new)), int(gone.sum())


@dataclass(frozen=True)
class HoldingsReport:
    market: str
    run: bool
    file_date: dt.date | None
    rows: int
    outcome: str  # stored | replaced | unchanged | planned | failed
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome != "failed"


def _digest(payload: Payload) -> str:
    import hashlib

    h = hashlib.sha256()
    for name in sorted(payload):
        h.update(name.encode("utf-8") + b"\0" + payload[name])
    return h.hexdigest()


def refresh_holdings(
    market: str,
    fetch: Callable[[], tuple[Payload, str] | tuple[Payload, str, dt.date]],
    root: Path,
    *,
    run: bool,
    today: dt.date | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> HoldingsReport:
    """Fetch ``market``'s export(s); with ``run`` add what is new to the store (never shrinking it).

    ``fetch`` may return a third item, the file date (a saved export's); else
    the fetch day is the file date, since neither regulator dates the file.
    """
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    target = HoldingsStore(root, market)
    try:
        fetched = fetch()
        payload, source = fetched[0], fetched[1]
        file_date = fetched[2] if len(fetched) > 2 else (today or clock().date())  # type: ignore[misc]
        if market == "nl":
            parsed = parse_holdings(payload["holdings"])
            fresh = notifications(parsed.lines)
            fresh["published_from"] = visible_from(fresh["obligation_date"])
            extras = {"lines": parsed.lines, "capital": parse_capital(payload["capital"])}
            key = NL_KEY
            parse_note = f"{len(parsed.lines):,} lines, {parsed.duplicates:,} duplicates, {parsed.dropped} stray rows"
        else:
            fresh = parse_de(payload["snapshot"])
            extras = {}
            key = DE_KEY
            parse_note = f"{len(fresh):,} snapshot rows"
    except (HoldingsFormatError, OSError, ValueError, KeyError) as exc:
        return HoldingsReport(market, run, None, 0, "failed", f"{type(exc).__name__}: {exc}")
    sha = _digest(payload)
    state = target.load_state()
    if state.get("sha256") == sha and target.exists():
        return HoldingsReport(market, run, file_date, int(state.get("rows", 0)), "unchanged")
    stored = target.read()
    rows, added, closed = accumulate(stored, fresh, key, file_date)
    detail = f"file dated {file_date.isoformat()}: {parse_note}; {added:,} new rows, {closed:,} closed" + (f", {len(stored):,} stored before" if stored is not None else "")
    if not run:
        return HoldingsReport(market, run, file_date, len(rows), "planned", detail)
    new_state = {
        "market": market,
        "kind": "holdings",
        "file_date": file_date.isoformat(),
        "rows": int(len(rows)),
        "sha256": sha,
        "bytes": int(sum(len(v) for v in payload.values())),
        "fetched_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": source,
        "detail": detail,
    }
    target.write(rows, new_state, extras)
    return HoldingsReport(market, run, file_date, len(rows), "replaced" if stored is not None else "stored", detail)


def status_holdings(root: Path) -> list[dict[str, Any]]:
    out = []
    for market in HOLDINGS_MARKETS:
        target = HoldingsStore(root, market)
        state = target.load_state()
        present = target.exists()
        out.append(
            {
                "market": market,
                "present": present,
                "file_date": state.get("file_date") if present else None,
                "rows": int(state.get("rows", 0)) if present else 0,
                "bytes": int(state.get("bytes", 0)) if present else 0,
                "last_fetch": state.get("fetched_at") if present else None,
            }
        )
    return out
