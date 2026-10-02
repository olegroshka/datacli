"""FINRA's public daily short sale volume files on ``cdn.finra.org``.

One text file per trade date and reporting-facility family, for example
``CNMSshvol20250102.txt`` (consolidated NMS). Layout, verified from 2018-08-01
to date: a header ``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market``,
pipe-delimited rows with ``YYYYMMDD`` dates, share volumes (whole shares until
early 2026, fractional shares since FINRA began reporting them) and a comma
list of facility codes, then a trailer line holding the row count.

A missing day (holiday, not yet published, before the first date) is an HTTP
403 whose body is S3's ``AccessDenied`` XML. Only that exact shape, or a 404,
means "absent"; any other 403 (a WAF block, a changed CDN) is an error, so an
outage can never be filed as a run of holidays.

The parser is strict on purpose: a changed layout must fail loudly with the
line and the rule it broke, never be misread into the store.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from finra.errors import CdnError, DailyFileFormatError

CDN_BASE = "https://cdn.finra.org/equity/regsho/daily"
DEFAULT_TIMEOUT = 60.0
HEADER = ("Date", "Symbol", "ShortVolume", "ShortExemptVolume", "TotalVolume", "Market")
ABSENT_MARKER = "<Code>AccessDenied</Code>"
# a plain non-negative decimal: FINRA reported whole shares until early 2026
# and fractional shares since; never 'nan', 'inf', '1e5' or a sign
_VOLUME = re.compile(r"^\d+(\.\d+)?$")

#: File-name prefix -> reporting facility family.
FAMILIES: dict[str, str] = {
    "CNMS": "Consolidated NMS (TRFs + ADF, exchange-listed)",
    "FNSQ": "FINRA/Nasdaq TRF Carteret",
    "FNYX": "FINRA/NYSE TRF",
    "FNQC": "FINRA/Nasdaq TRF Chicago",
    "FNRA": "ADF",
    "FORF": "ORF (OTC, non-NMS)",
}


@dataclass(frozen=True)
class DailyRow:
    symbol: str
    short_volume: float  # shares; whole until early 2026, fractional since
    short_exempt_volume: float
    total_volume: float
    facilities: str  # sorted facility codes joined by ',' e.g. 'B,N,Q'


@dataclass(frozen=True)
class DailyFile:
    family: str
    trade_date: date
    rows: tuple[DailyRow, ...]
    sha256: str  # of the raw bytes as served
    declared_count: int


def file_name(family: str, trade_date: date) -> str:
    return f"{family}shvol{trade_date.strftime('%Y%m%d')}.txt"


def file_url(family: str, trade_date: date, *, base_url: str = CDN_BASE) -> str:
    return f"{base_url.rstrip('/')}/{file_name(family, trade_date)}"


def is_absent(status: int, body: bytes | str) -> bool:
    """FINRA's "no such day": a 404, or a 403 carrying S3's AccessDenied XML."""
    if status == 404:
        return True
    if status != 403:
        return False
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return ABSENT_MARKER in text


def normalise_facilities(market: str) -> str:
    codes = sorted({code.strip() for code in market.split(",") if code.strip()})
    return ",".join(codes)


# --------------------------------------------------------------------------- #
# parser
# --------------------------------------------------------------------------- #
def parse_daily_file(raw: bytes, *, family: str, trade_date: date) -> DailyFile:
    """Parse one file strictly; raise :class:`DailyFileFormatError` on any deviation."""
    text = raw.decode("utf-8")
    lines = [line.rstrip("\r") for line in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 2:
        raise DailyFileFormatError(
            f"{family} {trade_date}: file has no header and trailer"
        )
    header = tuple(lines[0].split("|"))
    if header != HEADER:
        raise DailyFileFormatError(
            f"{family} {trade_date}: line 1: header {lines[0]!r} != {'|'.join(HEADER)!r}"
        )
    trailer = lines[-1].strip()
    if not trailer.isdigit():
        raise DailyFileFormatError(
            f"{family} {trade_date}: line {len(lines)}: trailer {trailer!r} is not a row count"
        )
    declared = int(trailer)
    expected_date = trade_date.strftime("%Y%m%d")
    rows: list[DailyRow] = []
    seen: set[str] = set()
    for number, line in enumerate(lines[1:-1], start=2):
        where = f"{family} {trade_date}: line {number}"
        fields = line.split("|")
        if len(fields) != len(HEADER):
            raise DailyFileFormatError(
                f"{where}: {len(fields)} fields, expected {len(HEADER)}"
            )
        day, symbol, short, exempt, total, market = (f.strip() for f in fields)
        if day != expected_date:
            raise DailyFileFormatError(f"{where}: date {day!r} != {expected_date}")
        if not symbol:
            raise DailyFileFormatError(f"{where}: empty symbol")
        if symbol in seen:
            raise DailyFileFormatError(f"{where}: duplicate symbol {symbol!r}")
        seen.add(symbol)
        if not all(_VOLUME.match(v) for v in (short, exempt, total)):
            raise DailyFileFormatError(f"{where}: non-numeric volume in {line!r}")
        short_n, exempt_n, total_n = float(short), float(exempt), float(total)
        if short_n > total_n:
            raise DailyFileFormatError(f"{where}: short {short} > total {total}")
        if exempt_n > short_n:
            raise DailyFileFormatError(f"{where}: exempt {exempt} > short {short}")
        facilities = normalise_facilities(market)
        if not facilities:
            raise DailyFileFormatError(f"{where}: empty market field")
        rows.append(DailyRow(symbol, short_n, exempt_n, total_n, facilities))
    if len(rows) != declared:
        raise DailyFileFormatError(
            f"{family} {trade_date}: trailer says {declared} rows, file holds {len(rows)}"
        )
    return DailyFile(
        family=family,
        trade_date=trade_date,
        rows=tuple(rows),
        sha256=hashlib.sha256(raw).hexdigest(),
        declared_count=declared,
    )


# --------------------------------------------------------------------------- #
# client
# --------------------------------------------------------------------------- #
class DailyFileClient:
    """Fetch and parse one daily file per call; ``None`` when FINRA has none."""

    def __init__(
        self,
        session: Any,
        *,
        base_url: str = CDN_BASE,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url
        self._timeout = timeout
        self._sleep = sleep
        self._log = log

    def fetch_raw(self, family: str, trade_date: date) -> bytes | None:
        """The file bytes, or ``None`` when the day is absent.

        Raises:
            CdnError: any other failure (a 403 that is not S3's AccessDenied,
                a 5xx after retries, a connection error after retries).
        """
        from _http import request_with_retry  # type: ignore[import-not-found]

        if family not in FAMILIES:
            raise ValueError(
                f"unknown file family {family!r}; known: {', '.join(FAMILIES)}"
            )
        url = file_url(family, trade_date, base_url=self._base_url)
        response = request_with_retry(
            self._session,
            "GET",
            url,
            timeout=self._timeout,
            log=self._log,
            label=file_name(family, trade_date),
            sleep=self._sleep,
        )
        status = int(response.status_code)
        if status == 200:
            return bytes(response.content)
        if is_absent(status, response.content):
            return None
        raise CdnError(
            f"{file_name(family, trade_date)}: unexpected response", status=status
        )

    def fetch_day(self, family: str, trade_date: date) -> DailyFile | None:
        raw = self.fetch_raw(family, trade_date)
        if raw is None:
            return None
        return parse_daily_file(raw, family=family, trade_date=trade_date)
