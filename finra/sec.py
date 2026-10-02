"""The SEC's fails-to-deliver files: the fourth transport on the FINRA source.

Twice a month the SEC posts a zip holding one pipe-delimited text file of
every CNS fail to deliver by settlement date and CUSIP:
``https://www.sec.gov/files/data/fails-deliver-data/cnsfails<YYYYMM>a.zip``
for the 1st to the 15th and ``...b.zip`` for the 16th to the month's end.
Layout, verified on 2018-08a, 2026-08b and 2026-09a: a header
``SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE``, rows
with ``YYYYMMDD`` dates and a price that may be ``.`` (none), then two
trailers, ``Trailer record count N`` and ``Trailer total quantity of shares
M``, both of which the parser checks. Files are UTF-8 with the odd cp1252
byte in old ones. ``(settlement date, CUSIP)`` is unique; a symbol can appear
twice on a day under two CUSIPs.

The SEC's automated-access policy refuses requests whose ``User-Agent``
does not declare who is asking and how to reach them, with its "Request Rate
Threshold Exceeded" page. The string is the owner's, read from
``SEC_USER_AGENT`` in the environment (process, then Windows user
environment) or ``[finra] sec_user_agent`` in the config; it is never written
into code, docs or logs. A missing file (a half not yet posted) is a 404.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from finra.auth import read_user_env
from finra.errors import DailyFileFormatError, SecError

SEC_BASE = "https://www.sec.gov/files/data/fails-deliver-data"
#: The SEC has posted some months under these paths instead (May 2026; Feb-Apr 2020).
SEC_ALTERNATE_BASES = (
    "https://www.sec.gov/files/data/other/fails-deliver-data",
    "https://www.sec.gov/files/node/add/data_distribution",
)
SEC_INDEX_URL = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
SEC_HOST = "https://www.sec.gov"
_INDEX_LINK = re.compile(r'href="([^"]*?/(cnsfails\d{6}[ab])(?:_\d+)?\.zip)"')


def parse_index(html: str) -> dict[str, str]:
    """``{half name: absolute url}`` for every fails file the listing page links.

    The first link for a half wins (the page lists newest first, and a
    re-posted file appears above its predecessor).
    """
    found: dict[str, str] = {}
    for match in _INDEX_LINK.finditer(html):
        href, name = match.group(1), match.group(2)
        url = href if href.startswith("http") else SEC_HOST + href
        found.setdefault(name, url)
    return found


USER_AGENT_VAR = "SEC_USER_AGENT"
CONFIG_KEY = "sec_user_agent"
DEFAULT_TIMEOUT = 120.0
HEADER = "SETTLEMENT DATE|CUSIP|SYMBOL|QUANTITY (FAILS)|DESCRIPTION|PRICE"
_COUNT_TRAILER = re.compile(r"^Trailer record count (\d+)$")
_QUANTITY_TRAILER = re.compile(r"^Trailer total quantity of shares (\d+)$")
_PRICE = re.compile(r"^\d+(\.\d+)?$")


def user_agent() -> str | None:
    """The owner's declared user agent, or ``None`` when nothing is configured."""
    value = read_user_env(USER_AGENT_VAR).strip()
    if value:
        return value
    try:
        import config as eodhd_config  # type: ignore[import-not-found]

        configured = eodhd_config.section("finra").get(CONFIG_KEY)
    except Exception:
        configured = None
    return str(configured).strip() if configured else None


def half_name(half_start: date) -> str:
    """``cnsfails202609a`` for 2026-09-01, ``cnsfails202609b`` for 2026-09-16."""
    if half_start.day not in (1, 16):
        raise ValueError(f"a half starts on the 1st or the 16th, not {half_start}")
    return f"cnsfails{half_start:%Y%m}{'a' if half_start.day == 1 else 'b'}"


def file_url(half_start: date, *, base_url: str = SEC_BASE) -> str:
    return f"{base_url.rstrip('/')}/{half_name(half_start)}.zip"


def half_bounds(half_start: date) -> tuple[date, date]:
    """The nominal range of a half: the 1st to the 15th, or the 16th to the end."""
    if half_start.day == 1:
        return half_start, half_start.replace(day=15)
    return half_start, month_bounds(half_start)[1]


def month_bounds(half_start: date) -> tuple[date, date]:
    """First and last day of the half's month: the range a file's dates must lie in.

    The split between the two files is the SEC's, not the calendar's: the
    July 2026 "b" file carries the 15th. So a file is checked against its
    month, and the two files of a month are checked against each other for
    duplicate ``(settlement date, CUSIP)`` rows in ``qc``.
    """
    from datetime import timedelta

    year, month = half_start.year, half_start.month
    first = date(year, month, 1)
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return first, last


@dataclass(frozen=True)
class FailRow:
    settlement_date: date
    cusip: str
    symbol: str
    quantity: int
    description: str
    price: float | None


@dataclass(frozen=True)
class FailsFile:
    half_start: date
    rows: tuple[FailRow, ...]
    sha256: str
    declared_count: int
    declared_quantity: int


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252")


def parse_fails_file(raw_zip: bytes, *, half_start: date) -> FailsFile:
    """Parse one zip strictly; raise :class:`DailyFileFormatError` on any deviation."""
    name = half_name(half_start)
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw_zip))
        members = archive.namelist()
        if len(members) != 1:
            raise DailyFileFormatError(
                f"{name}: zip holds {len(members)} members, expected 1"
            )
        text = _decode(archive.read(members[0]))
    except zipfile.BadZipFile:
        raise DailyFileFormatError(f"{name}: not a zip file") from None
    lines = [line.rstrip("\r") for line in text.split("\n")]
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 3:
        raise DailyFileFormatError(f"{name}: file has no header and trailers")
    if lines[0] != HEADER:
        raise DailyFileFormatError(f"{name}: line 1: header {lines[0]!r} != {HEADER!r}")
    count_match = _COUNT_TRAILER.match(lines[-2].strip())
    quantity_match = _QUANTITY_TRAILER.match(lines[-1].strip())
    if not count_match or not quantity_match:
        raise DailyFileFormatError(
            f"{name}: last two lines are not the record-count and quantity trailers"
        )
    declared_count, declared_quantity = int(count_match.group(1)), int(
        quantity_match.group(1)
    )
    first, last = month_bounds(half_start)
    rows: list[FailRow] = []
    seen: set[tuple[date, str]] = set()
    total = 0
    for number, line in enumerate(lines[1:-2], start=2):
        where = f"{name}: line {number}"
        fields = line.split("|")
        if len(fields) < 6:
            raise DailyFileFormatError(f"{where}: {len(fields)} fields, expected 6")
        # a description may itself contain a pipe ("DMY TECHNOLOGY GROUP INC IV | ",
        # April 2021); the fixed-position fields around it still validate
        fields = [*fields[:4], "|".join(fields[4:-1]), fields[-1]]
        day_s, cusip, symbol, quantity_s, description, price_s = (
            f.strip() for f in fields
        )
        try:
            day = (
                date(int(day_s[:4]), int(day_s[4:6]), int(day_s[6:8]))
                if len(day_s) == 8
                else None
            )
        except ValueError:
            day = None
        if day is None or not first <= day <= last:
            raise DailyFileFormatError(
                f"{where}: settlement date {day_s!r} outside {first}..{last}"
            )
        if not cusip:
            raise DailyFileFormatError(f"{where}: empty CUSIP in {line!r}")
        # a "CNS INELIGIBLE SECURITY" row has a CUSIP and no symbol; kept as published
        if (day, cusip) in seen:
            raise DailyFileFormatError(
                f"{where}: duplicate (settlement date, CUSIP) {day} {cusip}"
            )
        seen.add((day, cusip))
        if not quantity_s.isdigit():
            raise DailyFileFormatError(f"{where}: non-integer quantity in {line!r}")
        quantity = int(quantity_s)
        total += quantity
        if price_s in ("", "."):
            price: float | None = None
        elif _PRICE.match(price_s):
            price = float(price_s)
        else:
            raise DailyFileFormatError(f"{where}: non-numeric price {price_s!r}")
        rows.append(FailRow(day, cusip, symbol, quantity, description, price))
    if len(rows) != declared_count:
        raise DailyFileFormatError(
            f"{name}: trailer says {declared_count} rows, file holds {len(rows)}"
        )
    if total != declared_quantity:
        raise DailyFileFormatError(
            f"{name}: trailer says {declared_quantity} shares, rows sum to {total}"
        )
    return FailsFile(
        half_start=half_start,
        rows=tuple(rows),
        sha256=hashlib.sha256(raw_zip).hexdigest(),
        declared_count=declared_count,
        declared_quantity=declared_quantity,
    )


class SecFileClient:
    """Fetch and parse one half-month file per call; ``None`` when the SEC has none."""

    def __init__(
        self,
        session: Any,
        user_agent_string: str,
        *,
        base_url: str = SEC_BASE,
        alternate_bases: tuple[str, ...] = SEC_ALTERNATE_BASES,
        index_url: str = SEC_INDEX_URL,
        timeout: float = DEFAULT_TIMEOUT,
        sleep: Callable[[float], None] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if not user_agent_string.strip():
            raise ValueError("the SEC requires a declared user agent")
        self._session = session
        self._user_agent = user_agent_string.strip()
        self._base_url = base_url
        self._alternate_bases = tuple(alternate_bases)
        self._index_url = index_url
        self._index: dict[str, str] | None = None
        self._timeout = timeout
        self._sleep = sleep
        self._log = log

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": self._user_agent, "Accept-Encoding": "gzip, deflate"}

    def index(self) -> dict[str, str]:
        """``{half name: absolute url}`` from the SEC's listing page, fetched once.

        The SEC has posted files under three paths and once with a ``_0``
        suffix (``cnsfails201910a_0.zip``); the listing page links every
        file, so it is the authority when reachable. Empty when it is not.
        """
        if self._index is not None:
            return self._index
        from _http import request_with_retry  # type: ignore[import-not-found]

        found: dict[str, str] = {}
        try:
            response = request_with_retry(
                self._session,
                "GET",
                self._index_url,
                headers=self._headers(),
                timeout=self._timeout,
                log=self._log,
                label="SEC fails index",
                sleep=self._sleep,
            )
            if int(response.status_code) == 200:
                found = parse_index(response.content.decode("utf-8", "replace"))
        except Exception:  # the index is a convenience; the rule-based URLs remain
            found = {}
        self._index = found
        return found

    def candidate_urls(self, half_start: date) -> list[str]:
        """The listing page's URL first, then the rule-based paths."""
        urls: list[str] = []
        listed = self.index().get(half_name(half_start))
        if listed:
            urls.append(listed)
        for base in (self._base_url, *self._alternate_bases):
            url = file_url(half_start, base_url=base)
            if url not in urls:
                urls.append(url)
        return urls

    def fetch_raw(self, half_start: date) -> bytes | None:
        """The zip bytes, trying each candidate URL on a 404; ``None`` when none has it."""
        from _http import request_with_retry  # type: ignore[import-not-found]

        for url in self.candidate_urls(half_start):
            response = request_with_retry(
                self._session,
                "GET",
                url,
                headers=self._headers(),
                timeout=self._timeout,
                log=self._log,
                label=half_name(half_start),
                sleep=self._sleep,
            )
            if int(response.status_code) != 404:
                break
        else:
            return None
        status = int(response.status_code)
        if status == 200:
            return bytes(response.content)
        if status == 403:
            raise SecError(
                f"{half_name(half_start)}: the SEC refused the request; check SEC_USER_AGENT "
                "(their policy requires a declared name and contact) and the request rate",
                status=status,
            )
        raise SecError(f"{half_name(half_start)}: unexpected response", status=status)

    def fetch_half(self, half_start: date) -> FailsFile | None:
        raw = self.fetch_raw(half_start)
        if raw is None:
            return None
        return parse_fails_file(raw, half_start=half_start)
