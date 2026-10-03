"""The other European net short position registers: the Netherlands, Sweden, Norway, Ireland, Germany.

All publish under the same regulation as the FCA and the AMF: every holder
at or above 0.5 percent of the issuer's shares, disclosed by the next trading
day, a fall below the threshold disclosed once. None of these files carries
a publication date, so ``published_from`` is the next weekday after the
position date (the same rule as the FCA's, bank holidays ignored) and
``file_date`` is the latest position date in the file.

- ``nl`` (AFM): two CSV exports (history and current), semicolon-separated,
  Latin-1, decimal comma, ``Positie houder; Naam van de emittent; ISIN;
  Netto Shortpositie; Positiedatum``; history from 2012-11-01.
- ``se`` (Finansinspektionen): two ``.ods`` files (history and current),
  header row ``Innehavare av positionen, Namn pa emittent, ISIN, Position i
  procent, Datum for positionen, Kommentar`` after three title rows; history
  from 2010.
- ``no`` (Finanstilsynet): a JSON API, one entry per instrument with the
  aggregate's events and, per event, the active positions by holder with
  their own dates (from 2024-10). A holder present in one event and absent
  from the next is closed at the next event's date.
- ``ie`` (Central Bank of Ireland): one workbook, sheets ``Current`` and
  ``Historical``, header in the first row from the second column.
- ``de`` (Bundesanzeiger): a CSV behind a session; the plain download is
  the current positions, the history needs the site's filter form (the
  fetch tries it and falls back to the current list, saying which in the
  source label).

Each market's ``fetch`` returns a payload of named parts and ``parse`` takes
the same; :func:`registers.common.refresh` digests the parts together.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import re
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from typing import Any, Iterable

import pandas as pd

from registers import ods
from registers.common import Parsed, RegisterError, canonical, next_weekday

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) datacli registers (research)"
Payload = dict[str, bytes]

NL_HISTORY_URL = "https://www.afm.nl/export.aspx?type=3ca31b3d-23d9-4fa2-b846-29c7e3f0e5ff&format=csv"
NL_CURRENT_URL = "https://www.afm.nl/export.aspx?type=8a46a4ef-f196-4467-a7ab-1ae1cb58f0e7&format=csv"
NL_COLUMNS = ("Positie houder", "Naam van de emittent", "ISIN", "Netto Shortpositie", "Positiedatum")
SE_HISTORY_URL = "https://www.fi.se/BlankningsRegister/GetHistFile"
SE_CURRENT_URL = "https://www.fi.se/BlankningsRegister/GetAktuellFile"
SE_HEADER_FIRST = "Innehavare av positionen"
NO_API_URL = "https://ssr.finanstilsynet.no/api/v2/instruments"
IE_URL = (
    "https://www.centralbank.ie/docs/default-source/regulation/industry-market-sectors/securities-markets/"
    "short-selling-regulation/public-net-short-positions/table-of-significant-net-short-positions-in-shares.xlsx"
)
IE_COLUMNS = ("Position Holder:", "Name of the Issuer:", "ISIN:", "Net short position %:", "Position Date:")
DE_PAGE_URL = "https://www.bundesanzeiger.de/pub/en/nlp?0"
DE_COLUMNS = ("Positionsinhaber", "Emittent", "ISIN", "Position", "Datum")


def _get(url: str, *, timeout: float = 120.0, opener: Any = None, data: bytes | None = None) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, data=data)
    open_ = opener.open if opener is not None else urllib.request.urlopen
    with open_(request, timeout=timeout) as response:  # noqa: S310 (fixed https URLs)
        return response.read()


#: Rows without a holder, an ISIN, a position date or a percentage are dropped (file trailers,
#: the Swedish rows without an ISIN); more than this share of them, beyond a handful, is a format change.
MAX_DROPPED_SHARE = 0.02
MAX_DROPPED_ROWS = 10


def _finish(frame: pd.DataFrame, *, market: str) -> Parsed:
    """Common tail: drop the unusable rows, the T+1 publication rule, the file date, the canonical frame."""
    frame = frame.copy()
    for column in ("holder", "isin"):
        frame[column] = frame[column].astype("string").str.strip().replace({"": None})
    frame["position_date"] = pd.to_datetime(frame["position_date"], errors="coerce")
    usable = frame["holder"].notna() & frame["isin"].notna() & frame["position_date"].notna() & frame["net_short_pct"].notna()
    dropped = int((~usable).sum())
    if dropped > max(MAX_DROPPED_ROWS, MAX_DROPPED_SHARE * len(frame)):
        raise RegisterError(f"{dropped} of {len(frame)} rows without a holder, an ISIN, a position date or a percentage")
    frame = frame[usable].copy()
    if frame.empty:
        raise RegisterError("no usable rows")
    frame["position_date"] = frame["position_date"].dt.date
    frame["published_from"] = [next_weekday(d) for d in frame["position_date"]]
    frame["published_to"] = None
    frame = frame.drop_duplicates()
    file_date = max(frame["position_date"])
    return Parsed(file_date, canonical(frame, market=market, file_date=file_date), dropped=dropped)


def _decimal(series: pd.Series) -> pd.Series:
    """Decimal comma or point; a value written as ``<0,5`` (fell below the threshold, level undisclosed) is 0.0."""
    text = series.astype(str).str.strip().str.replace(",", ".", regex=False)
    below = text.str.startswith("<")
    return pd.to_numeric(text.where(~below, "0"), errors="coerce")


# --------------------------------------------------------------------------- #
# the Netherlands
# --------------------------------------------------------------------------- #
def fetch_nl() -> tuple[Payload, str]:
    return {"history": _get(NL_HISTORY_URL), "current": _get(NL_CURRENT_URL)}, f"{NL_HISTORY_URL} + current"


def parse_nl(payload: Payload) -> Parsed:
    frames = []
    for name, data in payload.items():
        try:
            frame = pd.read_csv(io.BytesIO(data), sep=";", encoding="latin-1", dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            raise RegisterError(f"{name}: not a readable CSV: {exc}") from None
        if tuple(frame.columns) != NL_COLUMNS:
            raise RegisterError(f"{name}: unexpected columns: {list(frame.columns)}")
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True).rename(
        columns={NL_COLUMNS[0]: "holder", NL_COLUMNS[1]: "issuer", NL_COLUMNS[2]: "isin", NL_COLUMNS[3]: "net_short_pct", NL_COLUMNS[4]: "position_date"}
    )
    frame["net_short_pct"] = _decimal(frame["net_short_pct"])
    frame["holder_lei"] = None
    return _finish(frame, market="nl")


# --------------------------------------------------------------------------- #
# Sweden
# --------------------------------------------------------------------------- #
def fetch_se() -> tuple[Payload, str]:
    return {"history": _get(SE_HISTORY_URL), "current": _get(SE_CURRENT_URL)}, f"{SE_HISTORY_URL} + current"


def _se_rows(data: bytes, name: str) -> pd.DataFrame:
    try:
        rows = ods.sheet_rows(data)
    except ValueError as exc:
        raise RegisterError(f"{name}: {exc}") from None
    start = next((i for i, r in enumerate(rows) if r and str(r[0]).strip() == SE_HEADER_FIRST), None)
    if start is None:
        raise RegisterError(f"{name}: header row '{SE_HEADER_FIRST}' not found")
    body = [r + [None] * (5 - len(r)) for r in rows[start + 1 :] if len(r) >= 3]
    return pd.DataFrame([r[:5] for r in body], columns=["holder", "issuer", "isin", "net_short_pct", "position_date"])


def parse_se(payload: Payload) -> Parsed:
    frame = pd.concat([_se_rows(data, name) for name, data in payload.items()], ignore_index=True)
    frame["net_short_pct"] = _decimal(frame["net_short_pct"])
    frame["position_date"] = frame["position_date"].astype(str).str.slice(0, 10)
    frame["issuer"] = frame["issuer"].astype(str).str.replace("\xa0", " ").str.strip()
    frame["holder_lei"] = None
    return _finish(frame, market="se")


# --------------------------------------------------------------------------- #
# Norway
# --------------------------------------------------------------------------- #
def fetch_no() -> tuple[Payload, str]:
    return {"api": _get(NO_API_URL)}, NO_API_URL


def norway_rows(instruments: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Holder rows from the API's per-instrument event lists, with closings at the next event."""
    rows: list[dict[str, Any]] = []
    for instrument in instruments:
        isin = str(instrument.get("isin", "")).strip()
        issuer = str(instrument.get("issuerName", "")).strip()
        events = sorted(instrument.get("events", []), key=lambda e: str(e.get("date", "")))
        previous: set[str] = set()
        for event in events:
            day = str(event.get("date", ""))[:10]
            active = event.get("activePositions") or []
            holders = set()
            for position in active:
                holder = str(position.get("positionHolder", "")).strip()
                holders.add(holder)
                rows.append({"holder": holder, "issuer": issuer, "isin": isin, "net_short_pct": position.get("shortPercent"),
                             "position_date": str(position.get("date", day))[:10]})
            for gone in previous - holders:
                rows.append({"holder": gone, "issuer": issuer, "isin": isin, "net_short_pct": 0.0, "position_date": day})
            previous = holders
    return rows


def parse_no(payload: Payload) -> Parsed:
    try:
        instruments = json.loads(payload["api"].decode("utf-8"))
    except (KeyError, ValueError) as exc:
        raise RegisterError(f"not the instruments JSON: {exc}") from None
    if not isinstance(instruments, list) or not instruments:
        raise RegisterError("the API returned no instruments")
    frame = pd.DataFrame(norway_rows(instruments))
    frame["net_short_pct"] = pd.to_numeric(frame["net_short_pct"], errors="coerce")
    frame["holder_lei"] = None
    return _finish(frame, market="no")


# --------------------------------------------------------------------------- #
# Ireland
# --------------------------------------------------------------------------- #
def fetch_ie() -> tuple[Payload, str]:
    return {"workbook": _get(IE_URL)}, IE_URL


def parse_ie(payload: Payload) -> Parsed:
    try:
        book = pd.ExcelFile(io.BytesIO(payload["workbook"]))
    except Exception as exc:  # noqa: BLE001
        raise RegisterError(f"not a readable workbook: {exc}") from None
    frames = []
    for sheet in ("Current", "Historical"):
        if sheet not in book.sheet_names:
            raise RegisterError(f"sheet {sheet!r} missing; found {book.sheet_names}")
        raw = book.parse(sheet, header=None)
        header = tuple(str(v).strip() for v in raw.iloc[0, 1:6])
        if header != IE_COLUMNS:
            raise RegisterError(f"{sheet}: unexpected header {header}")
        body = raw.iloc[1:, 1:6].copy()
        body.columns = ["holder", "issuer", "isin", "net_short_pct", "position_date"]
        frames.append(body[body["holder"].notna() & body["isin"].notna()])
    frame = pd.concat(frames, ignore_index=True)
    frame["net_short_pct"] = pd.to_numeric(frame["net_short_pct"], errors="coerce")
    frame["holder_lei"] = None
    return _finish(frame, market="ie")


# --------------------------------------------------------------------------- #
# Germany
# --------------------------------------------------------------------------- #
_DE_FORM = re.compile(r'<form class="search-form" id="\w+" method="post" action="([^"]+)"')
_DE_CSV = re.compile(r'href="([^"]*top~csv~form~panel-form-csv~resource~link[^"]*)"')


def fetch_de() -> tuple[Payload, str]:
    """The current CSV, and the history when the site's filter form accepts the flag."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
    page = _get(DE_PAGE_URL, opener=opener).decode("utf-8", "replace")
    form = _DE_FORM.search(page)
    link = _DE_CSV.search(page)
    if not link:
        raise RegisterError("the Bundesanzeiger page shows no CSV link")
    current = _get(link.group(1).replace("&amp;", "&"), opener=opener)
    payload: Payload = {"current": current}
    label = "bundesanzeiger current"
    if form:
        try:
            after = _get(form.group(1).replace("&amp;", "&"), opener=opener, data=urllib.parse.urlencode({"isin": "", "isHistorical": "true"}).encode()).decode("utf-8", "replace")
            link2 = _DE_CSV.search(after)
            if link2:
                history = _get(link2.group(1).replace("&amp;", "&"), opener=opener)
                if history.count(b"\n") > current.count(b"\n"):
                    payload["history"] = history
                    label = "bundesanzeiger current + history"
        except OSError:
            pass
    return payload, label


def parse_de(payload: Payload) -> Parsed:
    frames = []
    for name, data in payload.items():
        try:
            frame = pd.read_csv(io.BytesIO(data), sep=",", encoding="utf-8-sig", dtype=str, keep_default_na=False)
        except Exception as exc:  # noqa: BLE001
            raise RegisterError(f"{name}: not a readable CSV: {exc}") from None
        if tuple(frame.columns) != DE_COLUMNS:
            raise RegisterError(f"{name}: unexpected columns: {list(frame.columns)}")
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True).rename(
        columns={DE_COLUMNS[0]: "holder", DE_COLUMNS[1]: "issuer", DE_COLUMNS[2]: "isin", DE_COLUMNS[3]: "net_short_pct", DE_COLUMNS[4]: "position_date"}
    )
    frame["net_short_pct"] = _decimal(frame["net_short_pct"])
    frame["holder_lei"] = None
    return _finish(frame, market="de")


MARKETS: dict[str, tuple[Any, Any]] = {
    "nl": (fetch_nl, parse_nl),
    "se": (fetch_se, parse_se),
    "no": (fetch_no, parse_no),
    "ie": (fetch_ie, parse_ie),
    "de": (fetch_de, parse_de),
}
