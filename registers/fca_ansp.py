"""The FCA's aggregated net short positions (UK SSR 2025), archived as vintages.

From 13 July 2026 the FCA no longer names position holders. For every share on
its reportable shares list it publishes the **aggregated net short position**
(the sum of all notified positions of 0.2 percent or more, anonymised) in two
files that it regenerates on each publication day "from 12:00 onwards", two
working days after the position date (PS26/5 paragraph 6.12):

- ``aggregated-current-net-short-positions.csv``: one row per issuer with an
  open aggregate (company, ISIN, percentage, latest position date notified);
- ``aggregated-historic-net-short-positions.csv``: superseded aggregates with
  the date each became historical;
- ``aggregated-net-short-positions.xlsx``: both, as a workbook;
- ``uk-reportable-shares-list.csv`` / ``.xlsx``: the scope of the regime;
- ``short-positions-daily-update.xlsx``: the pre-reform per-holder workbook,
  frozen since 2026-07-11 (``registers.fca`` parses it); kept here so that any
  regeneration of the frozen file is noticed.

The FCA publishes no version history, the historic table may be amended in
place (SSR 6.4.1G-6.4.2G), and its CDN serves copies up to fourteen days old
(``Cache-Control: max-age=1209600``; ``Age`` of two days observed), so the only
record of what the public could see on a day is a copy retrieved that day
from the origin. This module therefore keeps **every distinct version** of
each file, content-addressed by sha256, with the server's ``Last-Modified``
(the publication timestamp), the retrieval time and the response headers, in
an append-only observation log. The files are stored as published; a light
parse records row counts, ISIN counts and the latest dates for status and QC.
Nothing here feeds the canonical per-holder history: the aggregate is a
different object (see ``notes`` on the measurement break).

Layout under ``<registers root>/uk_ansp/``::

    files/<key>/<stamp>__<sha256[:12]>.<ext>   one file per distinct content; stamp = the
                                               server's Last-Modified (UTC) when sent, else
                                               the retrieval or capture time
    observations.jsonl                         one line per file per retrieval: retrieved_at,
                                               key, url, source (live | wayback), sha256, bytes,
                                               the kept headers, changed, path, summary
    state.json                                 versions per key, latest per key (live retrievals only),
                                               last retrieval

``backfill_wayback`` pulls the Internet Archive's captures of the same URLs
(raw bytes through the ``id_`` flag) with their ``X-Archive-Orig-Last-Modified``
and ``Memento-Datetime`` headers into the same store, source ``wayback``: as of
2026-11-01 the archive held the current CSV as published on 17 and 27 July 2026 (captured 18 and 28 July), the
reportable shares list of April and September, and thirty earlier copies of the
per-holder workbook (October 2020 to 11 July 2026, one of them truncated by the archive).
"""

from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import io
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import pandas as pd

import _atomic  # type: ignore[import-not-found]

STORE = "uk_ansp"
_DOCS = "https://www.fca.org.uk/publication/documents/"
#: key -> (canonical URL, file extension). Order is the fetch order.
FILES: dict[str, tuple[str, str]] = {
    "current_csv": (_DOCS + "aggregated-current-net-short-positions.csv", "csv"),
    "historic_csv": (_DOCS + "aggregated-historic-net-short-positions.csv", "csv"),
    "ansp_xlsx": (_DOCS + "aggregated-net-short-positions.xlsx", "xlsx"),
    "rsl_csv": (_DOCS + "uk-reportable-shares-list.csv", "csv"),
    "rsl_xlsx": (_DOCS + "uk-reportable-shares-list.xlsx", "xlsx"),
    "named_xlsx": ("https://www.fca.org.uk/publication/data/short-positions-daily-update.xlsx", "xlsx"),
}
USER_AGENT = "datacli registers (research; see repository)"
#: Response headers kept in the observation log (lower-cased).
HEADERS_KEPT: tuple[str, ...] = ("last-modified", "date", "age", "etag", "content-type", "content-length", "cache-control")
WAYBACK_CDX = "https://web.archive.org/cdx/search/cdx"
WAYBACK_RAW = "https://web.archive.org/web/{timestamp}id_/{url}"
#: Stale after this many days without a retrieval (qc).
RETRIEVAL_STALE_DAYS = 3
#: The current file's Last-Modified older than this many days is a warning (qc): publication days are working days.
PUBLICATION_STALE_DAYS = 7


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _compact(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def parse_http_date(value: str | None) -> dt.datetime | None:
    """An RFC 7231 date header as an aware UTC datetime, or None when absent or unreadable."""
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Response:
    """One retrieved copy of one file."""

    data: bytes
    headers: dict[str, str]
    url: str
    source: str = "live"  # live | wayback
    #: The Internet Archive's capture time (ISO UTC) for a wayback response.
    capture_at: str | None = None
    #: The Internet Archive's capture timestamp (YYYYMMDDhhmmss) for a wayback response.
    wayback_timestamp: str | None = None

    @property
    def last_modified(self) -> dt.datetime | None:
        return parse_http_date(self.headers.get("last-modified"))


def _kept(headers: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    for name in HEADERS_KEPT:
        value = headers.get(name)
        if value is not None:
            out[name] = str(value)
    return out


def fetch(url: str, *, timeout: float = 120.0, now: Callable[[], dt.datetime] | None = None) -> Response:
    """The file from the FCA's origin.

    The FCA's CDN ignores ``Cache-Control: no-cache`` on the request and
    serves copies up to fourteen days old; a cache-busting query string
    (``?datacli=<epoch>``) makes it fetch from the origin, which returns the
    file with its real ``Last-Modified``. The canonical URL (without the
    query) is what the observation log records.
    """
    stamp = int((now or _utcnow)().timestamp())
    target = f"{url}{'&' if '?' in url else '?'}datacli={stamp}"
    request = urllib.request.Request(
        target, headers={"User-Agent": USER_AGENT, "Cache-Control": "no-cache", "Pragma": "no-cache"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URLs)
        data = response.read()
        headers = _kept(response.headers)
    return Response(data, headers, url)


def _get_text(url: str, *, timeout: float = 120.0) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URLs)
        return response.read().decode("utf-8", errors="replace")


def parse_cdx(text: str) -> list[tuple[str, str]]:
    """``(timestamp, digest)`` pairs of the 200 captures in a CDX ``txt`` listing (``fl=timestamp,statuscode,digest``)."""
    out: list[tuple[str, str]] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3 or parts[1] != "200":
            continue
        out.append((parts[0], parts[2]))
    return out


def wayback_captures(url: str, *, get: Callable[[str], str] = _get_text) -> list[tuple[str, str]]:
    """The Internet Archive's distinct 200 captures of ``url`` (collapsed on content digest), oldest first."""
    query = urllib.parse.urlencode(
        {
            "url": url.split("://", 1)[-1],
            "output": "txt",
            "fl": "timestamp,statuscode,digest",
            "filter": "statuscode:200",
            "collapse": "digest",
        }
    )
    return parse_cdx(get(f"{WAYBACK_CDX}?{query}"))


def wayback_headers(raw: Any) -> dict[str, str]:
    """The archived response's original headers, mapped onto the kept names (``X-Archive-Orig-*``)."""
    out: dict[str, str] = {}
    for name in HEADERS_KEPT:
        value = raw.get(f"x-archive-orig-{name}")
        if value is None and name == "content-type":
            value = raw.get("content-type")
        if value is not None:
            out[name] = str(value)
    return out


def fetch_wayback(url: str, timestamp: str, *, timeout: float = 180.0) -> Response:
    """One capture's original bytes and headers from the Internet Archive."""
    request = urllib.request.Request(WAYBACK_RAW.format(timestamp=timestamp, url=url), headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 (fixed https URLs)
        data = response.read()
        raw = response.headers
    memento = parse_http_date(raw.get("memento-datetime"))
    if memento is None:
        memento = dt.datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=dt.timezone.utc)
    return Response(data, wayback_headers(raw), url, source="wayback", capture_at=_iso(memento), wayback_timestamp=timestamp)


# --------------------------------------------------------------------------- #
# light parse
# --------------------------------------------------------------------------- #
def _max_date(values: pd.Series) -> str | None:
    parsed = pd.to_datetime(values, format="%d/%m/%Y", errors="coerce")
    if parsed.notna().any():
        return parsed.max().date().isoformat()
    return None


def summarize(key: str, data: bytes) -> dict[str, Any]:
    """Row counts and the latest dates of one file; a ``parse_error`` entry instead of raising."""
    try:
        if FILES.get(key, ("", ""))[1] == "csv" or key.endswith("_csv"):
            try:
                frame = pd.read_csv(io.BytesIO(data), encoding="utf-8-sig", dtype=str)
                encoding = "utf-8-sig"
            except UnicodeDecodeError:  # the reportable shares list of September 2026 was published in Windows-1252
                frame = pd.read_csv(io.BytesIO(data), encoding="cp1252", dtype=str)
                encoding = "cp1252"
            out: dict[str, Any] = {"rows": int(len(frame)), "columns": [str(c) for c in frame.columns], "encoding": encoding}
            dates = {c: _max_date(frame[c]) for c in frame.columns if "date" in c.lower()}
            out["max_dates"] = dates
            isin = next((c for c in frame.columns if "isin" in c.lower()), None)
            if isin is not None:
                out["n_isins"] = int(frame[isin].nunique())
            pct = next((c for c in frame.columns if "(%)" in c), None)
            if pct is not None:
                out["sum_pct"] = round(float(pd.to_numeric(frame[pct], errors="coerce").sum()), 4)
            return out
        book = pd.ExcelFile(io.BytesIO(data))
        return {"sheets": {name: int(len(book.parse(name))) for name in book.sheet_names}}
    except Exception as exc:  # noqa: BLE001 (a summary must never block the archive)
        return {"parse_error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# store
# --------------------------------------------------------------------------- #
class VintageStore:
    """The ``uk_ansp`` directory: content-addressed files, an observation log, a state sidecar."""

    def __init__(self, root: Path) -> None:
        self.dir = Path(root) / STORE
        self.files = self.dir / "files"
        self.log = self.dir / "observations.jsonl"
        self.state_path = self.dir / "state.json"

    def exists(self) -> bool:
        return self.state_path.exists()

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"versions": {}, "latest": {}}
        state = json.loads(self.state_path.read_text(encoding="utf-8"))
        state.setdefault("versions", {})
        state.setdefault("latest", {})
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        _atomic.replace_with_retry(tmp, self.state_path)

    @staticmethod
    def has_version(state: dict[str, Any], key: str, sha256: str) -> bool:
        return any(v.get("sha256") == sha256 for v in state["versions"].get(key, []))

    def add(self, state: dict[str, Any], key: str, response: Response, sha256: str, *, retrieved_at: str, summary: dict[str, Any]) -> Path:
        """Write the bytes once under ``files/<key>/`` and record the version in ``state``."""
        ext = FILES.get(key, ("", "bin"))[1]
        last_modified = response.last_modified
        if last_modified is not None:
            stamp = _compact(last_modified)
        elif response.capture_at:
            stamp = response.capture_at.replace("-", "").replace(":", "")
        else:
            stamp = retrieved_at.replace("-", "").replace(":", "")
        folder = self.files / key
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{stamp}__{sha256[:12]}.{ext}"
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(response.data)
        _atomic.replace_with_retry(tmp, path)
        state["versions"].setdefault(key, []).append(
            {
                "sha256": sha256,
                "bytes": len(response.data),
                "path": path.relative_to(self.dir).as_posix(),
                "last_modified": _iso(last_modified) if last_modified else None,
                "first_seen": retrieved_at,
                "source": response.source,
                "capture_at": response.capture_at,
                "summary": summary,
            }
        )
        return path

    def observe(self, record: dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")

    def observations(self) -> list[dict[str, Any]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class FileResult:
    key: str
    outcome: str  # stored | unchanged | planned | failed
    sha256: str | None = None
    bytes: int = 0
    last_modified: str | None = None
    detail: str = ""
    source: str = "live"


@dataclass(frozen=True)
class AnspReport:
    run: bool
    root: Path
    retrieved_at: str
    results: tuple[FileResult, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not any(r.outcome == "failed" for r in self.results)

    @property
    def stored(self) -> int:
        return sum(1 for r in self.results if r.outcome == "stored")


def _record(response: Response, sha256: str, key: str, *, retrieved_at: str, stored_path: Path | None, changed: bool, summary: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "retrieved_at": retrieved_at,
        "key": key,
        "url": response.url,
        "source": response.source,
        "capture_at": response.capture_at,
        "wayback_timestamp": response.wayback_timestamp,
        "sha256": sha256,
        "bytes": len(response.data),
        "headers": response.headers,
        "last_modified": _iso(response.last_modified) if response.last_modified else None,
        "changed": changed,
        "path": stored_path.relative_to(Path(stored_path).parents[2]).as_posix() if stored_path else None,
        "summary": summary,
    }


def _archive_one(
    store: VintageStore,
    state: dict[str, Any],
    key: str,
    response: Response,
    *,
    run: bool,
    retrieved_at: str,
) -> FileResult:
    sha256 = hashlib.sha256(response.data).hexdigest()
    last_modified = _iso(response.last_modified) if response.last_modified else None
    known = store.has_version(state, key, sha256)
    latest = {"sha256": sha256, "last_modified": last_modified, "observed_at": retrieved_at, "source": response.source}
    if known:
        if run:
            store.observe(_record(response, sha256, key, retrieved_at=retrieved_at, stored_path=None, changed=False, summary=None))
            if response.source == "live":  # `latest` is what the FCA serves now; archive captures never move it
                state["latest"][key] = latest
        return FileResult(key, "unchanged", sha256, len(response.data), last_modified, "already archived", response.source)
    if not run:
        return FileResult(key, "planned", sha256, len(response.data), last_modified, "new version; nothing written without --run", response.source)
    summary = summarize(key, response.data)
    path = store.add(state, key, response, sha256, retrieved_at=retrieved_at, summary=summary)
    store.observe(_record(response, sha256, key, retrieved_at=retrieved_at, stored_path=path, changed=True, summary=summary))
    if response.source == "live":
        state["latest"][key] = latest
    detail = "; ".join(
        part
        for part in (
            f"{summary['rows']:,} rows" if "rows" in summary else "",
            f"{summary['n_isins']:,} ISINs" if "n_isins" in summary else "",
            ("latest " + ", ".join(f"{k.split('(')[0].strip().lower()} {v}" for k, v in summary.get("max_dates", {}).items() if v)) if summary.get("max_dates") else "",
            f"sheets {summary['sheets']}" if "sheets" in summary else "",
            summary.get("parse_error", ""),
        )
        if part
    )
    return FileResult(key, "stored", sha256, len(response.data), last_modified, detail, response.source)


def refresh(
    root: Path,
    *,
    run: bool,
    keys: tuple[str, ...] | None = None,
    fetch_file: Callable[[str], Response] = fetch,
    now: Callable[[], dt.datetime] | None = None,
) -> AnspReport:
    """Retrieve every file from the FCA; with ``run`` archive each new version and log every retrieval.

    A transport error on one file does not stop the others. Without ``run``
    the files are downloaded and compared but nothing is written.
    """
    clock = now or _utcnow
    retrieved_at = _iso(clock())
    store = VintageStore(root)
    state = store.load_state()
    results: list[FileResult] = []
    for key in keys or tuple(FILES):
        url = FILES[key][0]
        try:
            response = fetch_file(url)
        except (OSError, ValueError) as exc:  # URLError and HTTPError are OSErrors
            results.append(FileResult(key, "failed", detail=f"{type(exc).__name__}: {exc}"))
            if run:
                store.observe({"retrieved_at": retrieved_at, "key": key, "url": url, "source": "live", "error": f"{type(exc).__name__}: {exc}"})
            continue
        results.append(_archive_one(store, state, key, response, run=run, retrieved_at=retrieved_at))
    if run:
        state["last_retrieval"] = retrieved_at
        state["last_retrieval_ok"] = all(r.outcome != "failed" for r in results)
        store.save_state(state)
    return AnspReport(run, Path(root), retrieved_at, tuple(results))


def backfill_wayback(
    root: Path,
    *,
    run: bool,
    keys: tuple[str, ...] | None = None,
    captures: Callable[[str], list[tuple[str, str]]] = wayback_captures,
    fetch_capture: Callable[[str, str], Response] = fetch_wayback,
    now: Callable[[], dt.datetime] | None = None,
    pause: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> AnspReport:
    """Archive the Internet Archive's captures of the files (one-off; every capture is one observation)."""
    clock = now or _utcnow
    retrieved_at = _iso(clock())
    store = VintageStore(root)
    state = store.load_state()
    results: list[FileResult] = []
    for key in keys or tuple(FILES):
        url = FILES[key][0]
        try:
            listing = captures(url)
        except (OSError, ValueError) as exc:
            results.append(FileResult(key, "failed", detail=f"CDX: {type(exc).__name__}: {exc}", source="wayback"))
            continue
        for index, (timestamp, _digest) in enumerate(listing):
            if index:
                sleep(pause)
            try:
                response = fetch_capture(url, timestamp)
            except (OSError, ValueError) as exc:
                results.append(FileResult(key, "failed", detail=f"capture {timestamp}: {type(exc).__name__}: {exc}", source="wayback"))
                continue
            result = _archive_one(store, state, key, response, run=run, retrieved_at=retrieved_at)
            results.append(FileResult(key, result.outcome, result.sha256, result.bytes, result.last_modified, f"capture {timestamp}: {result.detail}", "wayback"))
    if run:
        state["last_wayback_backfill"] = retrieved_at
        store.save_state(state)
    return AnspReport(run, Path(root), retrieved_at, tuple(results))


# --------------------------------------------------------------------------- #
# status / qc
# --------------------------------------------------------------------------- #
def status(root: Path) -> dict[str, Any]:
    store = VintageStore(root)
    if not store.exists():
        return {"present": False, "dir": str(store.dir), "files": []}
    state = store.load_state()
    files = []
    for key, (url, _ext) in FILES.items():
        versions = state["versions"].get(key, [])
        latest = state["latest"].get(key, {})
        newest = max((v for v in versions if v.get("last_modified")), key=lambda v: v["last_modified"], default=None)
        files.append(
            {
                "key": key,
                "url": url,
                "versions": len(versions),
                "live_versions": sum(1 for v in versions if v.get("source") == "live"),
                "wayback_versions": sum(1 for v in versions if v.get("source") == "wayback"),
                "earliest_last_modified": min((v["last_modified"] for v in versions if v.get("last_modified")), default=None),
                "latest_last_modified": newest["last_modified"] if newest else None,
                "latest_observed_at": latest.get("observed_at"),
                "bytes": sum(int(v.get("bytes", 0)) for v in versions),
                "unparsed": sum(1 for v in versions if isinstance(v.get("summary"), dict) and v["summary"].get("parse_error")),
            }
        )
    return {
        "present": True,
        "dir": str(store.dir),
        "last_retrieval": state.get("last_retrieval"),
        "last_retrieval_ok": state.get("last_retrieval_ok"),
        "last_wayback_backfill": state.get("last_wayback_backfill"),
        "observations": len(store.observations()),
        "files": files,
    }


def qc(root: Path, *, today: dt.date | None = None) -> list[tuple[str, str, str]]:
    """``(severity, check, detail)``; silent when the archive does not exist (it is optional)."""
    store = VintageStore(root)
    if not store.exists():
        return []
    today = today or dt.date.today()
    state = store.load_state()
    findings: list[tuple[str, str, str]] = []
    last = state.get("last_retrieval")
    if not last:
        findings.append(("warn", "uk_ansp_never_retrieved", "the archive exists but no live retrieval is recorded"))
    else:
        age = (today - dt.date.fromisoformat(last[:10])).days
        if age > RETRIEVAL_STALE_DAYS:
            findings.append(("warn", "uk_ansp_stale_retrieval", f"last retrieval {last[:10]}, {age} days ago; run `registers fetch --ansp --run`"))
    if state.get("last_retrieval_ok") is False:
        findings.append(("warn", "uk_ansp_last_retrieval_failed", "the last retrieval did not get every file; see observations.jsonl"))
    current = state["latest"].get("current_csv", {})
    if current.get("last_modified"):
        age = (today - dt.date.fromisoformat(current["last_modified"][:10])).days
        if age > PUBLICATION_STALE_DAYS:
            findings.append(("warn", "uk_ansp_stale_publication", f"the current file's Last-Modified is {current['last_modified'][:10]}, {age} days ago"))
    for key, versions in state["versions"].items():
        # live retrievals only: an Internet Archive capture can be truncated (one 2022 workbook capture stops at exactly 1 MiB)
        broken = [v for v in versions if v.get("source") == "live" and isinstance(v.get("summary"), dict) and v["summary"].get("parse_error")]
        if broken:
            findings.append(("warn", f"uk_ansp_{key}_unparsed", f"{len(broken)} archived version(s) did not parse: {broken[-1]['summary']['parse_error']}"))
    return findings
