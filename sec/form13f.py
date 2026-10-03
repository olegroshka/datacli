"""The SEC Form 13F data sets: listing, download, raw store.

The SEC publishes every 13F filing's cover page, summary and holdings table as
tab-separated files in zip archives: one per calendar quarter from 2013Q2 to
2023Q4 (``2013q2_form13f.zip``) and one per three months of **filing dates**
since (``01jun2026-31aug2026_form13f.zip``). The archives sit under more than
one URL prefix, so the links are read from the SEC's page rather than built.

Stored as published::

    <sec root>/form13f/<TABLE>/<archive>.parquet     every column a string
    <sec root>/form13f/form13f_fetch_state.csv       one row per archive

An archive is immutable once its period has passed; the newest one grows until
the SEC cuts the next, so it is re-fetched whenever its published size differs
from the stored one. The SEC requires a declared contact in the user agent
(``SEC_USER_AGENT``, read by ``finra.sec.user_agent``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import re
import tempfile
import zipfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import pandas as pd

NAME = "form13f"
PAGE_URL = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
SEC_HOST = "https://www.sec.gov"
TABLES: tuple[str, ...] = (
    "SUBMISSION",
    "COVERPAGE",
    "SUMMARYPAGE",
    "INFOTABLE",
    "OTHERMANAGER",
    "OTHERMANAGER2",
    "SIGNATURE",
)
#: Tables an archive must contain; the rest are stored when present.
REQUIRED_TABLES: tuple[str, ...] = ("SUBMISSION", "COVERPAGE", "INFOTABLE")
#: Derived per-filing table (see ``build_units``), stored next to the raw ones.
UNITS_TABLE = "FILING_UNITS"
#: First filing date on which the form asks for whole dollars, not thousands.
DOLLARS_FROM = dt.date(2023, 1, 3)
#: A filing whose values sit this far (log10) from the other filers' is in the other unit.
UNITS_LOG_GAP = 1.5
DEFAULT_TIMEOUT = 600.0
STATUS_OK = "ok"
STATUS_ERROR = "error"

_LINK = re.compile(r'href="([^"]*?/([0-9a-z-]+_form13f)\.zip)"', re.IGNORECASE)
_QUARTER = re.compile(r"^(\d{4})q([1-4])_form13f$")
_RANGE = re.compile(r"^(\d{2})([a-z]{3})(\d{4})-(\d{2})([a-z]{3})(\d{4})_form13f$")
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
)}


class Form13FError(Exception):
    """A listing, download or archive problem; the message is user-facing."""


@dataclass(frozen=True)
class Archive:
    """One published zip: ``name`` is the file stem, ``start``/``end`` its period."""

    name: str
    url: str
    start: dt.date
    end: dt.date


def archive_period(name: str) -> tuple[dt.date, dt.date]:
    """``2013q2_form13f`` or ``01jun2026-31aug2026_form13f`` -> its date range."""
    lowered = name.lower()
    quarter = _QUARTER.match(lowered)
    if quarter:
        year, q = int(quarter.group(1)), int(quarter.group(2))
        start = dt.date(year, 3 * q - 2, 1)
        end = dt.date(year + (q == 4), 3 * q % 12 + 1, 1) - dt.timedelta(days=1)
        return start, end
    span = _RANGE.match(lowered)
    if span:
        d1, m1, y1, d2, m2, y2 = span.groups()
        if m1 in _MONTHS and m2 in _MONTHS:
            return (
                dt.date(int(y1), _MONTHS[m1], int(d1)),
                dt.date(int(y2), _MONTHS[m2], int(d2)),
            )
    raise Form13FError(f"unrecognised 13F archive name: {name}")


def parse_listing(html: str) -> list[Archive]:
    """The archives linked from the SEC's page, oldest first."""
    found: dict[str, Archive] = {}
    for match in _LINK.finditer(html):
        href, name = match.group(1), match.group(2).lower()
        try:
            start, end = archive_period(name)
        except Form13FError:
            continue
        url = href if href.startswith("http") else SEC_HOST + href
        found.setdefault(name, Archive(name, url, start, end))
    return sorted(found.values(), key=lambda a: (a.start, a.end))


@dataclass(frozen=True)
class ArchiveState:
    archive: str
    status: str
    start: str = ""
    end: str = ""
    bytes: int = 0
    sha256: str = ""
    filings: int = 0
    holdings: int = 0
    fetched_at: str = ""
    detail: str = ""


STATE_COLUMNS = [f.name for f in fields(ArchiveState)]


class Store:
    """Parquet files per ``(table, archive)`` plus the state sidecar."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / NAME
        self.state_path = self.dir / f"{NAME}_fetch_state.csv"

    def table_dir(self, table: str) -> Path:
        return self.dir / table

    def table_path(self, table: str, archive: str) -> Path:
        return self.table_dir(table) / f"{archive}.parquet"

    def archives_on_disk(self) -> list[str]:
        base = self.table_dir("SUBMISSION")
        return sorted(p.stem for p in base.glob("*.parquet")) if base.is_dir() else []

    def load_state(self) -> dict[str, ArchiveState]:
        if not self.state_path.exists():
            return {}
        frame = pd.read_csv(self.state_path, dtype=str, keep_default_na=False)
        out: dict[str, ArchiveState] = {}
        for row in frame.to_dict(orient="records"):
            values = {k: row.get(k, "") for k in STATE_COLUMNS}
            for key in ("bytes", "filings", "holdings"):
                values[key] = int(float(values[key] or 0))
            out[str(values["archive"])] = ArchiveState(**values)
        return out

    def upsert_state(self, state: ArchiveState) -> None:
        import _atomic  # type: ignore[import-not-found]

        merged = self.load_state()
        merged[state.archive] = state
        rows = [asdict(merged[key]) for key in sorted(merged)]
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic.to_csv(pd.DataFrame(rows, columns=STATE_COLUMNS), self.state_path, index=False)


def plan(
    listed: Sequence[Archive],
    state: Mapping[str, ArchiveState],
    *,
    remote_bytes: Mapping[str, int] | None = None,
    full: bool = False,
    limit: int | None = None,
) -> list[Archive]:
    """Archives to fetch, newest first.

    An archive is due when it has no ``ok`` state row, or when the size the SEC
    now serves (``remote_bytes``, known for the newest archives only) differs
    from the stored one. ``limit`` caps the list from the newest end.
    """
    due: list[Archive] = []
    for archive in sorted(listed, key=lambda a: (a.start, a.end), reverse=True):
        known = state.get(archive.name)
        changed = (
            remote_bytes is not None
            and archive.name in remote_bytes
            and known is not None
            and known.bytes != remote_bytes[archive.name]
        )
        if full or known is None or known.status != STATUS_OK or changed:
            due.append(archive)
    return due[:limit] if limit else due


def convert_archive(zip_path: Path, archive: str, store: Store, *, con: Any = None) -> dict[str, int]:
    """Write each table of the zip as a parquet file; returns rows per table.

    Columns are kept exactly as published and as strings. Files are written to
    a temporary name and renamed, so a failed conversion leaves no partial
    table behind.
    """
    import duckdb

    own = con is None
    con = duckdb.connect() if own else con
    rows: dict[str, int] = {}
    try:
        with zipfile.ZipFile(zip_path) as archive_zip, tempfile.TemporaryDirectory(
            dir=store.dir
        ) as scratch:
            members = {Path(n).stem.upper(): n for n in archive_zip.namelist() if n.lower().endswith(".tsv")}
            missing = [t for t in REQUIRED_TABLES if t not in members]
            if missing:
                raise Form13FError(f"{archive}: archive lacks {', '.join(missing)}")
            written: list[tuple[Path, Path]] = []
            for table in TABLES:
                member = members.get(table)
                if member is None:
                    continue
                extracted = Path(archive_zip.extract(member, scratch))
                target = store.table_path(table, archive)
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = target.with_suffix(".parquet.tmp")
                con.execute(
                    "COPY (SELECT * FROM read_csv(?, delim='\t', header=true, all_varchar=true, "
                    "quote='', escape='', strict_mode=false, null_padding=true)) "
                    f"TO '{staged.as_posix()}' (FORMAT parquet, COMPRESSION zstd)",
                    [str(extracted)],
                )
                rows[table] = int(
                    con.execute(f"SELECT count(*) FROM read_parquet('{staged.as_posix()}')").fetchone()[0]
                )
                written.append((staged, target))
                extracted.unlink()
            for staged, target in written:
                staged.replace(target)
        rows[UNITS_TABLE] = build_units(store, archive, con=con)
    finally:
        if own:
            con.close()
    return rows


def build_units(store: Store, archive: str, *, con: Any = None) -> int:
    """Write ``FILING_UNITS/<archive>.parquet``: which unit each filing's values are in.

    The form switched from thousands of dollars to dollars for filings made on
    or after 2023-01-03, but some filers kept the old unit for a while and a
    few used dollars early. Within an archive most filings for a period agree,
    so each filing's median value-per-share is compared with the median over
    all filings of the same ``(cusip, period)``; a filing about three orders of
    magnitude below the crowd is in thousands when the crowd is in dollars,
    and the other way round. ``value_factor`` multiplies ``VALUE`` into
    dollars. Returns the number of filings classified.
    """
    import duckdb

    own = con is None
    con = duckdb.connect() if own else con
    info = store.table_path("INFOTABLE", archive).as_posix()
    sub = store.table_path("SUBMISSION", archive).as_posix()
    target = store.table_path(UNITS_TABLE, archive)
    target.parent.mkdir(parents=True, exist_ok=True)
    staged = target.with_suffix(".parquet.tmp")
    try:
        con.execute(f"""
            COPY (
              WITH rows AS (
                SELECT i.ACCESSION_NUMBER AS accession_number, i.CUSIP AS cusip,
                       s.PERIODOFREPORT AS period,
                       CAST(strptime(s.FILING_DATE, '%d-%b-%Y') AS DATE) AS filing_date,
                       TRY_CAST(i.VALUE AS DOUBLE) / TRY_CAST(i.SSHPRNAMT AS DOUBLE) AS ratio
                FROM read_parquet('{info}') i
                JOIN read_parquet('{sub}') s ON s.ACCESSION_NUMBER = i.ACCESSION_NUMBER
                WHERE i.SSHPRNAMTTYPE = 'SH' AND coalesce(i.PUTCALL, '') = ''
                  AND TRY_CAST(i.SSHPRNAMT AS DOUBLE) > 0 AND TRY_CAST(i.VALUE AS DOUBLE) > 0
              ), ref AS (
                SELECT cusip, period, median(ratio) AS crowd, count(DISTINCT accession_number) AS filers
                FROM rows GROUP BY 1, 2 HAVING count(DISTINCT accession_number) >= 5
              ), per_filing AS (
                SELECT r.accession_number, any_value(r.filing_date) AS filing_date,
                       median(log10(r.ratio / f.crowd)) AS log_gap, count(*) AS compared
                FROM rows r JOIN ref f USING (cusip, period) GROUP BY 1
              ), all_filings AS (
                SELECT ACCESSION_NUMBER AS accession_number,
                       CAST(strptime(FILING_DATE, '%d-%b-%Y') AS DATE) AS filing_date
                FROM read_parquet('{sub}')
              )
              SELECT a.accession_number,
                     a.filing_date >= DATE '{DOLLARS_FROM}' AS crowd_dollars,
                     p.log_gap, coalesce(p.compared, 0) AS compared,
                     CASE
                       WHEN p.log_gap IS NULL THEN 'assumed'
                       WHEN p.log_gap < -{UNITS_LOG_GAP} THEN 'below_crowd'
                       WHEN p.log_gap > {UNITS_LOG_GAP} THEN 'above_crowd'
                       ELSE 'with_crowd' END AS evidence,
                     CASE
                       WHEN a.filing_date >= DATE '{DOLLARS_FROM}'
                         THEN CASE WHEN coalesce(p.log_gap, 0) < -{UNITS_LOG_GAP} THEN 1000.0 ELSE 1.0 END
                       ELSE CASE WHEN coalesce(p.log_gap, 0) > {UNITS_LOG_GAP} THEN 1.0 ELSE 1000.0 END
                     END AS value_factor
              FROM all_filings a LEFT JOIN per_filing p USING (accession_number)
            ) TO '{staged.as_posix()}' (FORMAT parquet, COMPRESSION zstd)
            """)
        count = int(con.execute(f"SELECT count(*) FROM read_parquet('{staged.as_posix()}')").fetchone()[0])
        staged.replace(target)
    finally:
        if own:
            con.close()
    return count


@dataclass(frozen=True)
class FetchReport:
    run: bool
    root: Path
    listed: int
    planned: tuple[str, ...]
    stored: tuple[str, ...]
    failed: tuple[tuple[str, str], ...]
    holdings: int

    @property
    def ok(self) -> bool:
        return not self.failed


def _headers(user_agent: str) -> dict[str, str]:
    return {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}


def list_archives(session: Any, user_agent: str, *, timeout: float = 60.0) -> list[Archive]:
    response = session.get(PAGE_URL, headers=_headers(user_agent), timeout=timeout)
    if response.status_code != 200:
        raise Form13FError(
            f"the SEC listing page returned HTTP {response.status_code}; check SEC_USER_AGENT"
        )
    archives = parse_listing(response.text)
    if not archives:
        raise Form13FError("no 13F archives found on the SEC listing page (layout changed?)")
    return archives


def remote_size(session: Any, user_agent: str, archive: Archive, *, timeout: float = 60.0) -> int | None:
    response = session.head(
        archive.url, headers=_headers(user_agent), timeout=timeout, allow_redirects=True
    )
    if response.status_code != 200:
        return None
    try:
        return int(response.headers.get("Content-Length", ""))
    except ValueError:
        return None


def download(session: Any, user_agent: str, archive: Archive, target: Path, *, timeout: float = DEFAULT_TIMEOUT) -> tuple[int, str]:
    """Stream the zip to ``target``; returns ``(bytes, sha256)``."""
    response = session.get(
        archive.url, headers=_headers(user_agent), timeout=timeout, stream=True
    )
    if response.status_code != 200:
        raise Form13FError(f"{archive.name}: HTTP {response.status_code}")
    digest = hashlib.sha256()
    size = 0
    with open(target, "wb") as handle:
        for chunk in response.iter_content(chunk_size=1 << 20):
            if chunk:
                handle.write(chunk)
                digest.update(chunk)
                size += len(chunk)
    return size, digest.hexdigest()


def refresh(
    session: Any,
    user_agent: str,
    root: Path,
    *,
    run: bool,
    full: bool = False,
    limit: int | None = None,
    recheck: int = 2,
    sleep: Callable[[float], None] | None = None,
    log: Callable[[str], None] | None = None,
    now: Callable[[], dt.datetime] | None = None,
) -> FetchReport:
    """Plan and, with ``run``, fetch the due archives; one state row per archive.

    The sizes of the ``recheck`` newest listed archives are read with a HEAD
    request so a grown archive is picked up again. Each archive is converted
    and its state row written before the next download starts, so an
    interrupted backfill resumes where it stopped.
    """
    store = Store(root)
    say = log or (lambda _msg: None)
    clock = now or (lambda: dt.datetime.now(dt.timezone.utc))
    listed = list_archives(session, user_agent)
    state = store.load_state()
    sizes: dict[str, int] = {}
    for archive in listed[-recheck:] if recheck else []:
        if archive.name in state:
            size = remote_size(session, user_agent, archive)
            if size is not None:
                sizes[archive.name] = size
    due = plan(listed, state, remote_bytes=sizes, full=full, limit=limit)
    stored: list[str] = []
    failed: list[tuple[str, str]] = []
    holdings = 0
    if run:
        store.dir.mkdir(parents=True, exist_ok=True)
        for archive in due:
            stamp = clock().strftime("%Y-%m-%dT%H:%M:%SZ")
            zip_path = store.dir / f"_{archive.name}.zip.part"
            try:
                size, sha = download(session, user_agent, archive, zip_path)
                rows = convert_archive(zip_path, archive.name, store)
            except (Form13FError, zipfile.BadZipFile, OSError) as exc:
                failed.append((archive.name, str(exc)))
                store.upsert_state(
                    ArchiveState(archive.name, STATUS_ERROR, archive.start.isoformat(),
                                 archive.end.isoformat(), fetched_at=stamp, detail=str(exc)[:300])
                )
                say(f"{archive.name}: FAILED {exc}")
                continue
            except Exception as exc:  # a malformed table: record it, keep going
                failed.append((archive.name, f"{type(exc).__name__}: {exc}"))
                store.upsert_state(
                    ArchiveState(archive.name, STATUS_ERROR, archive.start.isoformat(),
                                 archive.end.isoformat(), fetched_at=stamp,
                                 detail=f"{type(exc).__name__}: {exc}"[:300])
                )
                say(f"{archive.name}: FAILED {type(exc).__name__}: {exc}")
                continue
            finally:
                zip_path.unlink(missing_ok=True)
            previous = state.get(archive.name)
            detail = ""
            if previous is not None and previous.status == STATUS_OK and previous.sha256 != sha:
                detail = f"republished {stamp[:10]}: was {previous.bytes} bytes, {previous.holdings} holdings"
            store.upsert_state(
                ArchiveState(
                    archive=archive.name,
                    status=STATUS_OK,
                    start=archive.start.isoformat(),
                    end=archive.end.isoformat(),
                    bytes=size,
                    sha256=sha,
                    filings=rows.get("SUBMISSION", 0),
                    holdings=rows.get("INFOTABLE", 0),
                    fetched_at=stamp,
                    detail=detail,
                )
            )
            stored.append(archive.name)
            holdings += rows.get("INFOTABLE", 0)
            say(f"{archive.name}: {rows.get('SUBMISSION', 0):,} filings, {rows.get('INFOTABLE', 0):,} holdings")
            if sleep is not None:
                sleep(1.0)
    return FetchReport(
        run=run,
        root=root,
        listed=len(listed),
        planned=tuple(a.name for a in due),
        stored=tuple(stored),
        failed=tuple(failed),
        holdings=holdings,
    )
