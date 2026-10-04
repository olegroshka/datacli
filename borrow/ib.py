"""Interactive Brokers' shortable list, ``usa.txt`` and the country files (DD-002 WP6, KB-003 section 4).

Anonymous FTP on ``ftp2.interactivebrokers.com`` (user ``shortstock``) serves
one file per country with an md5 companion, all in one format. ``usa.txt``
(about 1.8 MB, 20,000 rows) is regenerated through the day and carries no
history, so each fetch is a snapshot that is gone the next day unless stored::

    #BOF|2026.10.03|10:42:07
    #SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|
    A|USD|AGILENT TECHNOLOGIES INC|...|US00846U1016|3.6300|0.2500|7500000|BBG000C2V3D6|
    ...
    #EOF|20049

Every row ends with a pipe, rates are percent per year or ``NA``, the ISIN is
masked (``XXXXXXX...``) for many lines, ``AVAILABLE`` is capped as
``>10000000`` for the most liquid names (stored as the cap with
``available_capped`` true), and ``usa.txt`` also lists EUR lines.

Store: ``<borrow root>/ib/snapshots/YYYY-MM-DD.parquet`` keyed by the file's
own ``#BOF`` date (New York), one file per day, with the finra partition
store's state sidecar (sha256 of the raw bytes, row count, the file's
timestamp in ``detail``). A fetch on a day already stored with the same
content is a no-op; a different content on the same day replaces the file
(the broker's later state of the day) and the state row says so.

The country files (``COUNTRIES``: the European venues the public short
registers cover, WP21) are stored the same way as their own datasets,
``<borrow root>/ib_<country>/snapshots/``. They overlap heavily (each lists
the names tradable from that venue, in several currencies), so a reader
picking one rate per ISIN takes the home file first.
"""

from __future__ import annotations

import datetime as dt
import ftplib
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow as pa

from finra.store import STATUS_OK, DayState, DayStore

NAME = "ib"
HOST = "ftp2.interactivebrokers.com"
USER = "shortstock"
FILE = "usa.txt"
MD5_FILE = "usa.txt.md5"
SOURCE = "ftp"
#: The files captured: the US and the venues of the captured public short registers.
COUNTRIES: dict[str, str] = {
    "usa": "usa.txt",
    "germany": "germany.txt",
    "france": "france.txt",
    "dutch": "dutch.txt",
    "british": "british.txt",
    "swedish": "swedish.txt",
}
HEADER = "#SYM|CUR|NAME|CON|ISIN|REBATERATE|FEERATE|AVAILABLE|FIGI|"
_BOF = re.compile(r"^#BOF\|(\d{4})\.(\d{2})\.(\d{2})\|(\d{2}):(\d{2}):(\d{2})\s*$")
_EOF = re.compile(r"^#EOF\|(\d+)\s*$")
_MASKED_ISIN = re.compile(r"^X+")

SCHEMA = pa.schema(
    [
        ("snapshot_date", pa.date32()),
        ("symbol", pa.string()),
        ("currency", pa.string()),
        ("name", pa.string()),
        ("con_id", pa.int64()),
        ("isin", pa.string()),
        ("rebate_rate", pa.float64()),
        ("fee_rate", pa.float64()),
        ("available", pa.int64()),
        ("available_capped", pa.bool_()),
        ("figi", pa.string()),
    ]
)


class BorrowError(Exception):
    """A transport, checksum or format problem; the message is user-facing."""


@dataclass(frozen=True)
class Snapshot:
    """One parsed file: its ``#BOF`` timestamp and the rows."""

    stamp: dt.datetime
    rows: pd.DataFrame

    @property
    def date(self) -> dt.date:
        return self.stamp.date()


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def verify_md5(data: bytes, companion: str) -> None:
    """Raise unless the companion's hex digest matches ``data``.

    The broker computes the digest over LF line endings while the FTP server
    serves the file with CRLF (observed 2026-10-03: the raw bytes never match,
    the normalised bytes always do), so both forms are accepted.
    """
    expected = companion.strip().split()[0].lower() if companion.strip() else ""
    if not re.fullmatch(r"[0-9a-f]{32}", expected):
        raise BorrowError(f"the md5 companion is not a digest: {companion[:40]!r}")
    raw = md5_hex(data)
    normalised = md5_hex(data.replace(b"\r\n", b"\n"))
    if expected not in (raw, normalised):
        raise BorrowError(f"md5 mismatch: file {raw} (LF form {normalised}), companion {expected}")


def _number(value: str, *, field: str, line: int) -> float | None:
    value = value.strip()
    if value in ("", "NA"):
        return None
    try:
        return float(value)
    except ValueError:
        raise BorrowError(f"line {line}: {field} is not a number: {value!r}") from None


def _integer(value: str, *, field: str, line: int) -> int | None:
    number = _number(value, field=field, line=line)
    if number is None:
        return None
    if number != int(number):
        raise BorrowError(f"line {line}: {field} is not an integer: {value!r}")
    return int(number)


def parse(text: str) -> Snapshot:
    """Parse ``usa.txt`` strictly: the ``#BOF`` line, the exact header, ``#EOF`` with the row count."""
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if len(lines) < 3:
        raise BorrowError("the file has fewer than three lines")
    bof = _BOF.match(lines[0])
    if not bof:
        raise BorrowError(f"the first line is not a #BOF stamp: {lines[0][:60]!r}")
    y, mo, d, h, mi, s = (int(g) for g in bof.groups())
    stamp = dt.datetime(y, mo, d, h, mi, s)
    if lines[1].strip() != HEADER:
        raise BorrowError(f"unexpected header: {lines[1][:80]!r}")
    eof = _EOF.match(lines[-1])
    if not eof:
        raise BorrowError(f"the last line is not an #EOF trailer: {lines[-1][:60]!r}")
    body = lines[2:-1]
    declared = int(eof.group(1))
    if declared != len(body):
        raise BorrowError(f"#EOF declares {declared} rows, the file has {len(body)}")
    records: list[dict[str, Any]] = []
    for offset, raw in enumerate(body, start=3):
        if not raw.endswith("|"):
            raise BorrowError(f"line {offset}: no trailing pipe")
        fields = raw[:-1].split("|")
        if len(fields) != 9:
            raise BorrowError(f"line {offset}: expected 9 fields, got {len(fields)}")
        symbol, currency, name, con, isin, rebate, fee, available, figi = fields
        if not symbol.strip():
            raise BorrowError(f"line {offset}: empty symbol")
        capped = available.strip().startswith(">")
        if capped:
            available = available.strip()[1:]
        records.append(
            {
                "snapshot_date": stamp.date(),
                "symbol": symbol.strip(),
                "currency": currency.strip() or None,
                "name": name.strip() or None,
                "con_id": _integer(con, field="CON", line=offset),
                "isin": None if not isin.strip() or _MASKED_ISIN.match(isin.strip()) else isin.strip(),
                "rebate_rate": _number(rebate, field="REBATERATE", line=offset),
                "fee_rate": _number(fee, field="FEERATE", line=offset),
                "available": _integer(available, field="AVAILABLE", line=offset),
                "available_capped": capped,
                "figi": figi.strip() or None,
            }
        )
    frame = pd.DataFrame(records, columns=list(SCHEMA.names))
    return Snapshot(stamp, frame)


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def ftp_connect(host: str = HOST, user: str = USER, *, timeout: float = 120.0) -> ftplib.FTP:
    """An anonymous session on the broker's FTP host (the password is empty by design)."""
    ftp = ftplib.FTP(host, timeout=timeout)
    ftp.login(user, "")
    return ftp


def download(ftp: Any, name: str = FILE, md5_name: str = MD5_FILE) -> tuple[bytes, str]:
    """``(file bytes, md5 companion text)``; the caller verifies."""
    data = io.BytesIO()
    ftp.retrbinary(f"RETR {name}", data.write)
    companion = io.BytesIO()
    ftp.retrbinary(f"RETR {md5_name}", companion.write)
    return data.getvalue(), companion.getvalue().decode("ascii", "replace")


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
def dataset_name(country: str) -> str:
    if country not in COUNTRIES:
        raise BorrowError(f"unknown country {country!r}; expected one of {', '.join(COUNTRIES)}")
    return NAME if country == "usa" else f"{NAME}_{country}"


def store(root: Path, country: str = "usa") -> DayStore:
    return DayStore(Path(root), dataset_name(country), SCHEMA, subdir="snapshots")


def latest_snapshots(root: Path) -> dict[str, dt.date]:
    """The last stored day per country, for the countries with a snapshot."""
    out: dict[str, dt.date] = {}
    for country in COUNTRIES:
        days = store(root, country).days_on_disk()
        if days:
            out[country] = days[-1]
    return out


@dataclass(frozen=True)
class FetchReport:
    run: bool
    root: Path
    stamp: dt.datetime | None
    rows: int
    outcome: str  # stored | replaced | unchanged | planned | failed
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome != "failed"


def refresh(
    ftp_factory: Callable[[], Any],
    root: Path,
    *,
    run: bool,
    now: Callable[[], dt.datetime] | None = None,
    country: str = "usa",
) -> FetchReport:
    """Fetch the current file; with ``run`` store it as its ``#BOF`` date's snapshot.

    Without ``run`` the file is still downloaded and parsed (it is the only
    way to know its date and size) but nothing is written. ``country`` picks
    the file (``COUNTRIES``) and the dataset it is stored as.
    """
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    target = store(root, country)
    file = COUNTRIES[country]
    try:
        ftp = ftp_factory()
        try:
            data, companion = download(ftp, file, f"{file}.md5")
        finally:
            try:
                ftp.quit()
            except Exception:
                pass
        verify_md5(data, companion)
        snapshot = parse(data.decode("latin-1"))
    except (BorrowError, OSError, *ftplib.all_errors) as exc:  # type: ignore[misc]
        return FetchReport(run, root, None, 0, "failed", f"{type(exc).__name__}: {exc}")
    sha = hashlib.sha256(data).hexdigest()
    previous = target.load_state().get(snapshot.date.isoformat())
    if previous is not None and previous.sha256 == sha and target.day_path(snapshot.date).exists():
        return FetchReport(run, root, snapshot.stamp, len(snapshot.rows), "unchanged")
    if not run:
        return FetchReport(run, root, snapshot.stamp, len(snapshot.rows), "planned")
    target.write_day(snapshot.date, snapshot.rows)
    replaced = previous is not None and bool(previous.sha256)
    detail = f"file stamp {snapshot.stamp.isoformat()}"
    if replaced:
        detail += f"; replaced an earlier snapshot of the day (sha {previous.sha256[:12]})"
    target.upsert_state(
        [
            DayState(
                date=snapshot.date.isoformat(),
                status=STATUS_OK,
                source=SOURCE if country == "usa" else f"{SOURCE}:{file}",
                rows=int(len(snapshot.rows)),
                total_sum=int(len(data)),
                sha256=sha,
                fetched_at=clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                detail=detail,
            )
        ]
    )
    return FetchReport(run, root, snapshot.stamp, len(snapshot.rows), "replaced" if replaced else "stored", detail)


def status(root: Path, country: str = "usa") -> dict[str, Any]:
    target = store(root, country)
    days = target.days_on_disk()
    state = target.load_state()
    ok = [s for s in state.values() if s.status == STATUS_OK]
    return {
        "dataset": dataset_name(country),
        "present": bool(days),
        "snapshots": len(days),
        "first": days[0].isoformat() if days else None,
        "last": days[-1].isoformat() if days else None,
        "rows": sum(s.rows for s in ok),
        "bytes": sum(s.total_sum for s in ok),
        "replaced": sum(1 for s in ok if "replaced" in s.detail),
        "last_fetch": max((s.fetched_at for s in state.values()), default=None),
    }


def qc(root: Path, country: str = "usa") -> list[tuple[str, str, str]]:
    """``(severity, check, detail)`` over the stored snapshots."""
    target = store(root, country)
    days = target.days_on_disk()
    if not days:
        if country != "usa":
            return []
        return [("warn", "empty", "no borrow snapshots stored; run `borrow fetch --run`")]
    state = target.load_state()
    findings: list[tuple[str, str, str]] = []
    missing_state = [d for d in days if d.isoformat() not in state]
    if missing_state:
        findings.append(("error", "state_missing", f"{len(missing_state)} snapshots without a state row"))
    missing_file = [k for k, s in state.items() if s.status == STATUS_OK and s.day not in set(days)]
    if missing_file:
        findings.append(("error", "file_missing", f"{len(missing_file)} state rows without a file"))
    frame = target.read_range()
    dupes = int(frame.duplicated(["snapshot_date", "symbol", "currency"]).sum())
    if dupes:
        findings.append(("error", "duplicate_key", f"{dupes} duplicate (date, symbol, currency) rows"))
    if len(days) > 1:
        gaps = [(a, b) for a, b in zip(days, days[1:]) if (b - a).days > 4]
        if gaps:
            findings.append(("warn", "gaps", f"{len(gaps)} gaps of more than four days; latest before {gaps[-1][1]}"))
    stale = (dt.date.today() - days[-1]).days
    if stale > 4:
        findings.append(("warn", "stale", f"the last snapshot is {stale} days old; run `borrow fetch --run`"))
    name = dataset_name(country)
    return [(severity, check if country == "usa" else f"{name}.{check}", detail) for severity, check, detail in findings]
