"""SEC Form ADV adviser reports: monthly snapshots of every registered adviser.

The SEC publishes, roughly monthly since 2006, one zip holding one CSV with a
row per SEC-registered investment adviser and the Form ADV Part 1 answers:
``Organization CRD#``, names, ``5F(2)`` regulatory assets, ``5D`` client
types and the section 7B private-fund summary (``Any Hedge Funds``, ``Total
number of Hedge funds``, ``Total Gross Assets of Private Funds``). The CRD
number joins to the 13F cover page, so a snapshot dated before a filing says
whether the filer ran hedge funds at the time (INV-002 R4, WP8).

Stored as published::

    <sec root>/adv/snapshots/<name>.parquet      every column a string
    <sec root>/adv/adv_fetch_state.csv           one row per snapshot

``<name>`` is the SEC's file stem (``ia100226`` = 2026-10-02). The exempt
reporting adviser files (``*-exempt.zip``) are not fetched.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import re
import zipfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

from sec.form13f import SEC_HOST, _headers, download

NAME = "adv"
PAGE_URL = (
    "https://www.sec.gov/data-research/sec-markets-data/"
    "information-about-registered-investment-advisers-exempt-reporting-advisers"
)
STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_ERROR = "error"
_LINK = re.compile(r'href="([^"]*?/(ia(\d{6}|\d{8}))\.zip)"', re.IGNORECASE)
CRD_COLUMN = "Organization CRD#"


class AdvError(Exception):
    """A listing, download or archive problem; the message is user-facing."""


class AdvPlaceholder(AdvError):
    """The SEC published a notice instead of data (the 2019 shutdown): absent, not an error."""


@dataclass(frozen=True)
class Snapshot:
    name: str
    url: str
    date: dt.date


def snapshot_date(name: str) -> dt.date:
    """``ia100226`` (MMDDYY) or ``ia09012026`` (MMDDYYYY) -> the report date."""
    digits = name.lower().removeprefix("ia")
    if len(digits) == 6:
        return dt.datetime.strptime(digits, "%m%d%y").date()
    if len(digits) == 8:
        return dt.datetime.strptime(digits, "%m%d%Y").date()
    raise AdvError(f"unrecognised ADV snapshot name: {name}")


def parse_listing(html: str) -> list[Snapshot]:
    found: dict[str, Snapshot] = {}
    for match in _LINK.finditer(html):
        href, name = match.group(1), match.group(2).lower()
        try:
            when = snapshot_date(name)
        except (AdvError, ValueError):
            continue
        url = href if href.startswith("http") else SEC_HOST + href
        found.setdefault(name, Snapshot(name, url, when))
    return sorted(found.values(), key=lambda s: s.date)


@dataclass(frozen=True)
class SnapshotState:
    snapshot: str
    status: str
    date: str = ""
    bytes: int = 0
    sha256: str = ""
    advisers: int = 0
    hedge_fund_advisers: int = 0
    fetched_at: str = ""
    detail: str = ""


STATE_COLUMNS = [f.name for f in fields(SnapshotState)]


class Store:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / NAME
        self.snapshot_dir = self.dir / "snapshots"
        self.state_path = self.dir / f"{NAME}_fetch_state.csv"

    def path(self, name: str) -> Path:
        return self.snapshot_dir / f"{name}.parquet"

    def snapshots_on_disk(self) -> list[str]:
        if not self.snapshot_dir.is_dir():
            return []
        return sorted(p.stem for p in self.snapshot_dir.glob("*.parquet"))

    def load_state(self) -> dict[str, SnapshotState]:
        if not self.state_path.exists():
            return {}
        frame = pd.read_csv(self.state_path, dtype=str, keep_default_na=False)
        out: dict[str, SnapshotState] = {}
        for row in frame.to_dict(orient="records"):
            values = {k: row.get(k, "") for k in STATE_COLUMNS}
            for key in ("bytes", "advisers", "hedge_fund_advisers"):
                values[key] = int(float(values[key] or 0))
            out[str(values["snapshot"])] = SnapshotState(**values)
        return out

    def upsert_state(self, state: SnapshotState) -> None:
        import _atomic  # type: ignore[import-not-found]

        merged = self.load_state()
        merged[state.snapshot] = state
        rows = [asdict(merged[key]) for key in sorted(merged)]
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic.to_csv(pd.DataFrame(rows, columns=STATE_COLUMNS), self.state_path, index=False)


def read_member(name: str, data: bytes) -> pd.DataFrame:
    """One snapshot's table as strings, whatever the SEC packed it as.

    Three layouts over the years: a pipe-delimited ``.txt`` (2006 to 2008), an
    ``.xlsx`` workbook (2009 to early 2023) and a comma-separated ``.csv``
    (since late 2025). The first row is always the header.
    """
    lowered = name.lower()
    if lowered.endswith(".xlsx"):
        import openpyxl

        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        rows = book.worksheets[0].iter_rows(values_only=True)
        header = ["" if c is None else str(c) for c in next(rows)]
        body = [["" if c is None else str(c) for c in row] for row in rows]
        book.close()
        return pd.DataFrame(body, columns=header)
    if lowered.endswith(".txt"):
        # pipe-delimited with a trailing pipe; a few 2007-2008 rows carry one
        # pipe too many (a pipe inside a field), kept by dropping the surplus
        text = data.decode("latin-1")
        lines = [line for line in text.splitlines() if line.strip()]
        header = [c for c in lines[0].rstrip("|").split("|")]
        width = len(header)
        body = [row.rstrip("|").split("|")[:width] for row in lines[1:]]
        body = [row + [""] * (width - len(row)) for row in body]
        return pd.DataFrame(body, columns=header)
    frame = pd.read_csv(
        io.BytesIO(data),
        sep=",",
        dtype=str,
        encoding="latin-1",
        keep_default_na=False,
        low_memory=False,
    )
    unnamed = [c for c in frame.columns if str(c).startswith("Unnamed")]
    return frame.drop(columns=unnamed) if unnamed else frame


def normalise_columns(columns: Sequence[Any]) -> list[str]:
    """``Organization CRD #`` (2006) and ``Organization CRD#`` (today) are one column."""
    return [" ".join(str(c).split()).replace(" #", "#") for c in columns]


def dedupe_columns(columns: Sequence[str]) -> list[str]:
    """A header repeated in one file (``5H`` twice in 2013-2014) gets ``.1``, ``.2`` (pandas' convention)."""
    seen: dict[str, int] = {}
    out: list[str] = []
    for column in columns:
        seen[column] = seen.get(column, 0) + 1
        out.append(column if seen[column] == 1 else f"{column}.{seen[column] - 1}")
    return out


def convert_snapshot(zip_bytes: bytes, name: str, store: Store) -> tuple[int, int]:
    """Write the zip's table as one parquet file; returns ``(advisers, hedge fund advisers)``.

    Every column is kept as a string under its published name (whitespace
    normalised), plus ``snapshot_date`` from the file name. The zip must hold
    exactly one table.
    """
    import _atomic  # type: ignore[import-not-found]

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        members = [
            n for n in archive.namelist()
            if n.lower().endswith((".csv", ".txt", ".xlsx")) and not n.startswith("__MACOSX")
        ]
        if len(members) != 1:
            raise AdvError(f"{name}: expected one table in the zip, found {archive.namelist()}")
        frame = read_member(members[0], archive.read(members[0]))
    frame.columns = dedupe_columns(normalise_columns(frame.columns))
    if CRD_COLUMN not in frame.columns:
        first = " ".join(str(c) for c in list(frame.columns)[:3])
        if "unavailable" in first.lower():
            raise AdvPlaceholder(f"{name}: {first[:160]}")
        raise AdvError(f"{name}: column {CRD_COLUMN!r} missing; columns start {list(frame.columns)[:5]}")
    frame = frame[frame[CRD_COLUMN].str.strip() != ""]
    frame.insert(0, "snapshot_date", snapshot_date(name).isoformat())
    store.snapshot_dir.mkdir(parents=True, exist_ok=True)
    _atomic.to_parquet(frame.reset_index(drop=True), store.path(name), index=False)
    hedge = int((frame.get("Any Hedge Funds", pd.Series(dtype=str)).str.upper() == "Y").sum())
    return len(frame), hedge


def plan(
    listed: Sequence[Snapshot],
    state: Mapping[str, SnapshotState],
    *,
    full: bool = False,
    limit: int | None = None,
) -> list[Snapshot]:
    """Snapshots without an ``ok`` row (or all, with ``full``), newest first."""
    due = [
        s
        for s in sorted(listed, key=lambda s: s.date, reverse=True)
        if full or state.get(s.name) is None or state[s.name].status == STATUS_ERROR
    ]
    return due[:limit] if limit else due


@dataclass(frozen=True)
class FetchReport:
    run: bool
    root: Path
    listed: int
    planned: tuple[str, ...]
    stored: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]

    @property
    def ok(self) -> bool:
        return not self.failed


def list_snapshots(session: Any, user_agent: str, *, timeout: float = 60.0) -> list[Snapshot]:
    response = session.get(PAGE_URL, headers=_headers(user_agent), timeout=timeout)
    if response.status_code != 200:
        raise AdvError(f"the SEC listing page returned HTTP {response.status_code}; check SEC_USER_AGENT")
    listed = parse_listing(response.text)
    if not listed:
        raise AdvError("no ADV snapshots found on the SEC listing page (layout changed?)")
    return listed


def refresh(
    session: Any,
    user_agent: str,
    root: Path,
    *,
    run: bool,
    full: bool = False,
    limit: int | None = None,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> FetchReport:
    store = Store(root)
    say = log or (lambda _m: None)
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    listed = list_snapshots(session, user_agent)
    due = plan(listed, store.load_state(), full=full, limit=limit)
    stored: list[str] = []
    failed: list[tuple[str, str]] = []
    if run:
        store.dir.mkdir(parents=True, exist_ok=True)
        for snapshot in due:
            stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
            part = store.dir / f"_{snapshot.name}.zip.part"
            try:
                size, sha = download(session, user_agent, snapshot, part)  # type: ignore[arg-type]
                advisers, hedge = convert_snapshot(part.read_bytes(), snapshot.name, store)
            except AdvPlaceholder as exc:
                store.upsert_state(
                    SnapshotState(snapshot.name, STATUS_ABSENT, snapshot.date.isoformat(),
                                  fetched_at=stamp, detail=str(exc)[:300])
                )
                say(f"{snapshot.name}: absent ({exc})")
                continue
            except Exception as exc:
                failed.append((snapshot.name, f"{type(exc).__name__}: {exc}"))
                store.upsert_state(
                    SnapshotState(snapshot.name, STATUS_ERROR, snapshot.date.isoformat(),
                                  fetched_at=stamp, detail=f"{type(exc).__name__}: {exc}"[:300])
                )
                say(f"{snapshot.name}: FAILED {type(exc).__name__}: {exc}")
                continue
            finally:
                part.unlink(missing_ok=True)
            store.upsert_state(
                SnapshotState(
                    snapshot=snapshot.name,
                    status=STATUS_OK,
                    date=snapshot.date.isoformat(),
                    bytes=size,
                    sha256=sha,
                    advisers=advisers,
                    hedge_fund_advisers=hedge,
                    fetched_at=stamp,
                )
            )
            stored.append(snapshot.name)
            say(f"{snapshot.name} ({snapshot.date}): {advisers:,} advisers, {hedge:,} with hedge funds")
            if sleep is not None:
                sleep(0.5)
    return FetchReport(run, root, len(listed), tuple(s.name for s in due), tuple(stored), tuple(failed))
