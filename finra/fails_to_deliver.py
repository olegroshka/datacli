"""SEC fails to deliver: the fourth dataset, from the SEC's half-month files.

One row per ``(settlement_date, cusip)``: the CNS fail quantity, the symbol
and description as the SEC prints them, and the prior close used for the
file. The unit of work and of storage is one half-month file, kept as the
partition dated by the half's first day (the 1st or the 16th).

Point in time: the SEC posts the first half "at the end of the month" and
the second half "about the 15th of the next month". ``published_at`` is set
at ingest from that rule plus a five-day margin, which is conservative on
purpose: a row is never treated as known before the SEC could have posted
it. Join on ``published_at``.

Symbols carry no separators (``BRKB``), like short interest; the views map
them through the SIP spellings in the daily short volume store.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

import pandas as pd
import pyarrow as pa

from finra import calendar as cal
from finra import registry as reg
from finra.sec import FailsFile, SecFileClient, half_bounds
from finra.store import (
    STATUS_ABSENT,
    STATUS_ERROR,
    STATUS_OK,
    DayState,
    DayStore,
    partition_key,
)

SPEC = reg.spec("fails_to_deliver")
SOURCE_SEC = "sec"
PUBLICATION_MARGIN_DAYS = 5
DEFAULT_OVERLAP_DAYS = (
    35  # halves published this recently are re-fetched (the SEC revises)
)

COLUMNS = [
    "settlement_date",
    "cusip",
    "symbol",
    "quantity",
    "description",
    "price",
    "half_start",
    "published_at",
    "source",
]
SCHEMA = pa.schema(
    [
        ("settlement_date", pa.date32()),
        ("cusip", pa.string()),
        ("symbol", pa.string()),
        ("quantity", pa.int64()),
        ("description", pa.string()),
        ("price", pa.float64()),
        ("half_start", pa.date32()),
        ("published_at", pa.date32()),
        ("source", pa.string()),
    ]
)


def store(root: Path) -> DayStore:
    return DayStore(root, SPEC.name, SCHEMA, subdir=SPEC.subdir)


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def halves(start: date, end: date) -> list[date]:
    """Every half-month start (the 1st and the 16th) whose half overlaps ``[start, end]``."""
    out: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        for day in (1, 16):
            half = date(year, month, day)
            lo, hi = half_bounds(half)
            if hi >= start and lo <= end:
                out.append(half)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def published_on(half_start: date) -> date:
    """The SEC's posting rule plus the safety margin."""
    _, last = half_bounds(half_start)
    if half_start.day == 1:
        _, month_end = half_bounds(half_start.replace(day=16))
        base = month_end
    else:
        nxt = date(
            half_start.year + (half_start.month == 12), half_start.month % 12 + 1, 15
        )
        base = nxt
    return base + timedelta(days=PUBLICATION_MARGIN_DAYS)


def is_due(half_start: date, now: datetime) -> bool:
    return cal.to_et(now).date() >= published_on(half_start)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def frame_from_file(parsed: FailsFile) -> pd.DataFrame:
    published = published_on(parsed.half_start)
    rows = [
        {
            "settlement_date": r.settlement_date,
            "cusip": r.cusip,
            "symbol": r.symbol,
            "quantity": r.quantity,
            "description": r.description,
            "price": r.price,
            "half_start": parsed.half_start,
            "published_at": published,
            "source": SOURCE_SEC,
        }
        for r in parsed.rows
    ]
    out = pd.DataFrame(rows, columns=COLUMNS)
    for col in ("settlement_date", "half_start", "published_at"):
        out[col] = pd.to_datetime(out[col]).dt.date
    for col in ("cusip", "symbol", "description", "source"):
        out[col] = out[col].astype("string")
    out["quantity"] = out["quantity"].astype("int64")
    out["price"] = pd.to_numeric(out["price"], errors="coerce").astype("float64")
    return out.sort_values(["settlement_date", "cusip"]).reset_index(drop=True)[COLUMNS]


def frame_digest(frame: pd.DataFrame) -> str:
    payload = (
        frame[COLUMNS[:-1]].to_csv(index=False, lineterminator="\n").encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
class Transport(Protocol):
    name: str

    def fetch_partition(self, half_start: date) -> pd.DataFrame | None:
        """The half's canonical frame, or ``None`` when the SEC has not posted it."""


class SecTransport:
    name = SOURCE_SEC

    def __init__(self, client: SecFileClient) -> None:
        self._client = client

    def fetch_partition(self, half_start: date) -> pd.DataFrame | None:
        parsed = self._client.fetch_half(half_start)
        return None if parsed is None else frame_from_file(parsed)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def plan_partitions(
    *,
    from_date: date,
    to_date: date,
    state: Mapping[str, DayState],
    now: datetime,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    retry_absent: bool = False,
    full_refresh: bool = False,
) -> list[date]:
    """Half-month starts to fetch, newest first (due, and not already ``ok`` unless recent)."""
    today = cal.to_et(now).date()
    planned: list[date] = []
    for half in halves(from_date, to_date):
        if not is_due(half, now):
            continue
        prior = state.get(partition_key(half))
        recent = overlap_days > 0 and published_on(half) >= today - timedelta(
            days=overlap_days
        )
        if full_refresh or prior is None or prior.status == STATUS_ERROR:
            planned.append(half)
        elif recent:
            planned.append(half)
        elif prior.status == STATUS_ABSENT and retry_absent:
            planned.append(half)
    planned.sort(reverse=True)
    return planned


@dataclass(frozen=True)
class RefreshReport:
    dataset: str
    run: bool
    source: str
    root: Path
    planned: tuple[str, ...]
    stored: tuple[str, ...] = ()
    restated: tuple[str, ...] = ()
    absent: tuple[str, ...] = ()
    failed: tuple[tuple[str, str], ...] = ()
    rows: int = 0
    aborted: str = ""
    not_yet_publishable: date | None = None
    skipped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.aborted


def refresh(
    *,
    run: bool,
    root: Path,
    transport: Transport,
    from_date: date | None = None,
    to_date: date | None = None,
    limit_days: int | None = None,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    retry_absent: bool = False,
    full_refresh: bool = False,
    now: datetime | None = None,
    log: logging.Logger | None = None,
    progress: Callable[[str], None] | None = None,
) -> RefreshReport:
    """Plan (and with ``run``) fetch half-month files into ``root``; ``limit_days`` caps partitions."""
    moment = now if now is not None else cal.now_et()
    start = from_date or SPEC.first_date
    end = to_date or cal.to_et(moment).date()
    day_store = store(root)
    state = day_store.load_state()
    planned = plan_partitions(
        from_date=start,
        to_date=end,
        state=state,
        now=moment,
        overlap_days=overlap_days,
        retry_absent=retry_absent,
        full_refresh=full_refresh,
    )
    keys = [partition_key(h) for h in planned]
    skipped: tuple[str, ...] = ()
    if limit_days is not None and limit_days >= 0 and len(planned) > limit_days:
        planned, skipped = planned[:limit_days], tuple(keys[limit_days:])
        keys = keys[:limit_days]
    base = dict(
        dataset=SPEC.name,
        run=run,
        source=transport.name,
        root=Path(root),
        planned=tuple(keys),
        skipped=skipped,
    )
    if not run:
        return RefreshReport(**base)  # type: ignore[arg-type]
    stored: list[str] = []
    restated: list[str] = []
    absent: list[str] = []
    failed: list[tuple[str, str]] = []
    rows = 0
    say = progress or (lambda _m: None)
    for half, key in zip(planned, keys):
        prior = state.get(key)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            frame = transport.fetch_partition(half)
        except Exception as exc:
            why = f"{type(exc).__name__}: {exc}"
            failed.append((key, why))
            day_store.upsert_state(
                [
                    DayState(
                        key,
                        STATUS_ERROR,
                        transport.name,
                        fetched_at=stamp,
                        detail=why[:300],
                    )
                ]
            )
            say(f"{key}  error  {why}")
            if log:
                log.warning("%s: %s", key, why)
            continue
        if frame is None:
            absent.append(key)
            day_store.upsert_state(
                [
                    DayState(
                        key,
                        STATUS_ABSENT,
                        transport.name,
                        fetched_at=stamp,
                        detail="file not posted",
                    )
                ]
            )
            say(f"{key}  absent")
            continue
        digest = frame_digest(frame)
        detail = ""
        if (
            prior is not None
            and prior.status == STATUS_OK
            and prior.sha256
            and prior.sha256 != digest
        ):
            restated.append(key)
            detail = f"restated {stamp[:10]}: previously rows={prior.rows} sha256={prior.sha256[:12]}"
        day_store.write_day(half, frame)
        day_store.upsert_state(
            [
                DayState(
                    key,
                    STATUS_OK,
                    transport.name,
                    rows=int(len(frame)),
                    short_sum=int(frame["quantity"].sum()),
                    total_sum=int(frame["settlement_date"].nunique()),
                    sha256=digest,
                    fetched_at=stamp,
                    detail=detail
                    or (prior.detail if prior and prior.status == STATUS_OK else ""),
                )
            ]
        )
        stored.append(key)
        rows += int(len(frame))
        say(
            f"{key}  ok  {len(frame):,} rows"
            + ("  (restated)" if detail.startswith("restated") else "")
        )
        if log:
            log.info("%s: %d rows%s", key, len(frame), " (restated)" if detail else "")
    return RefreshReport(
        **base,  # type: ignore[arg-type]
        stored=tuple(stored),
        restated=tuple(restated),
        absent=tuple(absent),
        failed=tuple(failed),
        rows=rows,
    )


# --------------------------------------------------------------------------- #
# quality checks
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Finding:
    severity: str
    check: str
    message: str
    count: int = 0


@dataclass(frozen=True)
class QcReport:
    dataset: str
    days: int
    first: date | None
    last: date | None
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


def qc(root: Path, *, now: datetime | None = None, **_: Any) -> QcReport:
    day_store = store(root)
    days = day_store.days_on_disk()
    state = day_store.load_state()
    findings: list[Finding] = []
    if not days:
        return QcReport(
            SPEC.name,
            0,
            None,
            None,
            (Finding("info", "store", "no half-month files on disk"),),
        )
    for half in days:
        key = partition_key(half)
        frame = day_store.read_day(half)
        if frame is None or list(frame.columns) != COLUMNS:
            findings.append(
                Finding(
                    "error", "schema", f"{key}: columns differ from the pinned schema"
                )
            )
            continue
        dup = int(frame.duplicated(subset=["settlement_date", "cusip"]).sum())
        if dup:
            findings.append(
                Finding(
                    "error",
                    "duplicates",
                    f"{key}: duplicate (settlement date, CUSIP)",
                    dup,
                )
            )
        lo, hi = half_bounds(half)
        sd = pd.to_datetime(frame["settlement_date"]).dt.date
        off = int(((sd < lo) | (sd > hi)).sum())
        if off:
            findings.append(
                Finding(
                    "error",
                    "partition",
                    f"{key}: settlement dates outside the half",
                    off,
                )
            )
        wrong_pub = int(
            (pd.to_datetime(frame["published_at"]).dt.date != published_on(half)).sum()
        )
        if wrong_pub:
            findings.append(
                Finding(
                    "error",
                    "published_at",
                    f"{key}: published_at differs from the rule",
                    wrong_pub,
                )
            )
        neg = int((frame["quantity"] < 0).sum())
        if neg:
            findings.append(
                Finding("error", "negative", f"{key}: negative quantities", neg)
            )
        prior = state.get(key)
        if prior is None or prior.status != STATUS_OK:
            findings.append(
                Finding(
                    "warn",
                    "state",
                    f"{key}: file on disk but state is {prior.status if prior else 'missing'}",
                )
            )
        elif prior.rows != len(frame):
            findings.append(
                Finding(
                    "warn",
                    "state",
                    f"{key}: state says {prior.rows} rows, file holds {len(frame)}",
                )
            )
    for key, st in state.items():
        if st.status == STATUS_OK and not day_store.day_path(st.day).exists():
            findings.append(
                Finding("error", "state", f"{key}: state ok but no file on disk")
            )
    errors = sorted(k for k, st in state.items() if st.status == STATUS_ERROR)
    if errors:
        findings.append(
            Finding(
                "warn",
                "errors",
                f"halves in error: {', '.join(errors[-5:])}",
                len(errors),
            )
        )
    moment = now if now is not None else cal.now_et()
    on_disk = set(days)
    gaps = [
        h.isoformat()
        for h in halves(days[0], days[-1])
        if is_due(h, moment)
        and h not in on_disk
        and (
            state.get(partition_key(h)) is None
            or state[partition_key(h)].status == STATUS_ERROR
        )
    ]
    if gaps:
        findings.append(
            Finding(
                "warn",
                "gaps",
                f"due halves neither stored nor absent: {', '.join(gaps[-5:])}",
                len(gaps),
            )
        )
    return QcReport(SPEC.name, len(days), days[0], days[-1], tuple(findings))
