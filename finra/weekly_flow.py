"""Weekly ATS / OTC trade flow: the second FINRA dataset (entitled, API-only).

FINRA's OTC transparency data gives, for every week and every NMS symbol,
the trades and shares executed on each ATS (dark pool) and by each OTC
market maker, plus per-symbol and per-firm totals. One row per published
record is stored exactly as published; the symbol keeps its spelling and the
``summary_type`` tells the row kinds apart (per symbol per firm, per symbol,
per firm, market-wide).

The unit of work is one API partition, ``(week_start, tier)``. Publication
lags by tier are the point-in-time fact: Tier 1 appears on the Monday three
weeks after the week's Monday, Tier 2 on the Monday five weeks after.
``published_at`` carries FINRA's own ``initialPublishedDate`` per row, which
is the column to join on for anything that must not look ahead;
``updated_at`` moves when a firm restates.

:func:`refresh` mirrors :func:`finra.short_volume.refresh`: plan the due
partitions newest first, fetch each, write it and its state row before the
next, re-check a trailing overlap for restatements, and never re-probe an
``absent`` partition outside that window unless asked.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import pandas as pd
import pyarrow as pa

from finra import calendar as cal
from finra import registry as reg
from finra.api import CompareFilter, Filters, QueryApiClient
from finra.errors import FinraError
from finra.store import (
    STATUS_ABSENT,
    STATUS_ERROR,
    STATUS_OK,
    DayState,
    DayStore,
    partition_key,
)

SPEC = reg.spec("weekly_flow")
SOURCE_API = "api"

#: Days from the week's Monday to the Monday FINRA first publishes the tier.
PUBLICATION_LAG_DAYS: dict[str, int] = {"T1": 21, "T2": 35, "OTCE": 35}
DEFAULT_OVERLAP_DAYS = 14  # partitions this recent are re-fetched for restatements

COLUMNS = [
    "week_start",
    "tier",
    "summary_type",
    "symbol",
    "issue_name",
    "mpid",
    "firm_crd",
    "participant_name",
    "product_type",
    "trade_count",
    "share_quantity",
    "notional",
    "published_at",
    "updated_at",
    "last_reported_at",
    "source",
]
SCHEMA = pa.schema(
    [
        ("week_start", pa.date32()),
        ("tier", pa.string()),
        ("summary_type", pa.string()),
        ("symbol", pa.string()),
        ("issue_name", pa.string()),
        ("mpid", pa.string()),
        ("firm_crd", pa.int64()),
        ("participant_name", pa.string()),
        ("product_type", pa.string()),
        ("trade_count", pa.int64()),
        ("share_quantity", pa.float64()),
        ("notional", pa.float64()),
        ("published_at", pa.date32()),
        ("updated_at", pa.date32()),
        ("last_reported_at", pa.date32()),
        ("source", pa.string()),
    ]
)

# API field names (verified against the metadata endpoint, 2026-10-01)
API_FIELDS = {
    "weekStartDate": "week_start",
    "tierIdentifier": "tier",
    "summaryTypeCode": "summary_type",
    "issueSymbolIdentifier": "symbol",
    "issueName": "issue_name",
    "MPID": "mpid",
    "firmCRDNumber": "firm_crd",
    "marketParticipantName": "participant_name",
    "productTypeCode": "product_type",
    "totalWeeklyTradeCount": "trade_count",
    "totalWeeklyShareQuantity": "share_quantity",
    "totalNotionalSum": "notional",
    "initialPublishedDate": "published_at",
    "lastUpdateDate": "updated_at",
    "lastReportedDate": "last_reported_at",
}
SYMBOL_TYPES = ("ATS_W_SMBL", "OTC_W_SMBL")  # the per-symbol totals


# --------------------------------------------------------------------------- #
# symbol mapping: this feed uses the listing market's spelling (BRK.B, BF.A)
# --------------------------------------------------------------------------- #
_SEPARATORS = "./"


def security_kind(symbol: str) -> str:
    """Like :func:`finra.short_volume.security_kind`, accepting ``.`` as well as ``/``."""
    from finra.short_volume import security_kind as sip_kind

    return sip_kind(symbol.replace(".", "/"))


def eodhd_code(symbol: str) -> str | None:
    """``BRK.B`` and ``BRK/B`` both map to ``BRK-B``; NULL for non-common kinds."""
    if security_kind(symbol) != "common":
        return None
    return symbol.replace("/", "-").replace(".", "-")


def store(root: Path) -> DayStore:
    return DayStore(root, SPEC.name, SCHEMA, subdir=SPEC.subdir)


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def week_monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def weeks(start: date, end: date) -> list[date]:
    """Every week Monday from the week of ``start`` to the week of ``end``."""
    cursor = week_monday(start)
    last = week_monday(end)
    out: list[date] = []
    while cursor <= last:
        out.append(cursor)
        cursor += timedelta(days=7)
    return out


def first_publication(week_start: date, tier: str) -> date:
    """The Monday FINRA first publishes ``tier`` for the week starting ``week_start``."""
    return week_start + timedelta(days=PUBLICATION_LAG_DAYS[tier])


def is_due(week_start: date, tier: str, now: datetime) -> bool:
    return cal.to_et(now).date() >= first_publication(week_start, tier)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def frame_from_api_rows(
    week_start: date, tier: str, rows: Sequence[Mapping[str, Any]]
) -> pd.DataFrame:
    """The canonical frame for one partition; rows of other partitions are an error."""
    if not rows:
        return _finish(pd.DataFrame(columns=COLUMNS))
    frame = pd.DataFrame(list(rows))
    missing = [c for c in API_FIELDS if c not in frame.columns]
    if missing:
        raise ValueError(f"API rows lack fields {missing}")
    frame = frame[list(API_FIELDS)].rename(columns=API_FIELDS)
    wrong = frame[
        (frame["week_start"].astype(str) != week_start.isoformat())
        | (frame["tier"].astype(str) != tier)
    ]
    if not wrong.empty:
        raise ValueError(
            f"API rows carry partition {wrong['week_start'].iloc[0]}/{wrong['tier'].iloc[0]}, "
            f"expected {week_start}/{tier}"
        )
    frame["source"] = SOURCE_API
    return _finish(frame)


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in ("week_start", "published_at", "updated_at", "last_reported_at"):
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.date
    for col in (
        "tier",
        "summary_type",
        "symbol",
        "issue_name",
        "mpid",
        "participant_name",
        "product_type",
        "source",
    ):
        out[col] = out[col].astype("string")
    out["firm_crd"] = pd.to_numeric(out["firm_crd"], errors="coerce").astype("Int64")
    out["trade_count"] = (
        pd.to_numeric(out["trade_count"], errors="coerce").fillna(0).astype("int64")
    )
    for col in ("share_quantity", "notional"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    bad = out[(out["trade_count"] < 0) | (out["share_quantity"] < 0)]
    if not bad.empty:
        raise ValueError(f"{len(bad)} rows with negative trades or shares")
    # FINRA's identity is the *issue*: ATS rows are identified by MPID, OTC firm
    # rows by CRD number (no MPID); a symbol that changes listing tape mid-week
    # has one row per product type; a ticker reassigned to another issuer inside
    # the week (RNA, 2026-02) has one row per issue name.
    key = ["summary_type", "symbol", "mpid", "firm_crd", "product_type", "issue_name"]
    if out.duplicated(subset=key).any():
        raise ValueError(
            "duplicate (summary_type, symbol, mpid, firm_crd, product_type, "
            "issue_name) rows in one partition"
        )
    return out.sort_values(key, na_position="first").reset_index(drop=True)[COLUMNS]


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

    def fetch_partition(self, week_start: date, tier: str) -> pd.DataFrame | None:
        """The partition's canonical frame, or ``None`` when FINRA has no rows for it."""


class ApiTransport:
    name = SOURCE_API

    def __init__(self, client: QueryApiClient) -> None:
        self._client = client

    def fetch_partition(self, week_start: date, tier: str) -> pd.DataFrame | None:
        filters = Filters(
            compare=(
                CompareFilter(SPEC.partition_field, week_start.isoformat()),
                CompareFilter(SPEC.part_field, tier),
            )
        )
        rows = self._client.query(SPEC.api_group, SPEC.api_name, filters=filters)
        return None if not rows else frame_from_api_rows(week_start, tier, rows)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
Partition = tuple[date, str]


def plan_partitions(
    *,
    from_date: date,
    to_date: date,
    state: Mapping[str, DayState],
    now: datetime,
    tiers: Sequence[str] = SPEC.parts,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    retry_absent: bool = False,
    full_refresh: bool = False,
) -> list[Partition]:
    """``(week_start, tier)`` partitions to fetch, newest week first.

    A partition is planned when it is due (its first publication Monday has
    passed), and either has no ``ok`` row, or is ``error``, or its week starts
    inside the trailing ``overlap_days`` of the latest due week (restatements
    and late publication), or is ``absent`` with ``retry_absent``.
    """
    today = cal.to_et(now).date()
    planned: list[Partition] = []
    for week in weeks(from_date, to_date):
        for tier in tiers:
            if not is_due(week, tier, now):
                continue
            latest_due = week_monday(today - timedelta(days=PUBLICATION_LAG_DAYS[tier]))
            overlap_from = latest_due - timedelta(days=max(overlap_days, 0))
            prior = state.get(partition_key(week, tier))
            if full_refresh or prior is None or prior.status == STATUS_ERROR:
                planned.append((week, tier))
            elif overlap_days > 0 and week >= overlap_from:
                planned.append((week, tier))
            elif prior.status == STATUS_ABSENT and retry_absent:
                planned.append((week, tier))
    planned.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return planned


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RefreshReport:
    """What one ``refresh`` call planned and, with ``run``, did (keys are ``week/tier``)."""

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
    """Plan (and with ``run``) fetch weekly flow partitions into ``root``.

    ``limit_days`` caps the number of **partitions** this run (the CLI flag
    keeps its name across datasets). Each partition is written and recorded
    before the next, so an interrupted backfill resumes where it stopped.
    """
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
    keys = [partition_key(w, t) for w, t in planned]
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

    for (week, tier), key in zip(planned, keys):
        prior = state.get(key)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            frame = transport.fetch_partition(week, tier)
        except (
            Exception
        ) as exc:  # FinraError, a parse error, a connection error after retries
            why = f"{type(exc).__name__}: {exc}"
            failed.append((key, why))
            day_store.upsert_state(
                [
                    DayState(
                        week.isoformat(),
                        STATUS_ERROR,
                        transport.name,
                        fetched_at=stamp,
                        detail=why[:300],
                        part=tier,
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
                        week.isoformat(),
                        STATUS_ABSENT,
                        transport.name,
                        fetched_at=stamp,
                        detail="no rows published",
                        part=tier,
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
        day_store.write_day(week, frame, part=tier)
        day_store.upsert_state(
            [
                DayState(
                    week.isoformat(),
                    STATUS_OK,
                    transport.name,
                    rows=int(len(frame)),
                    short_sum=int(round(frame["trade_count"].sum())),
                    total_sum=int(round(frame["share_quantity"].sum())),
                    sha256=digest,
                    fetched_at=stamp,
                    detail=detail
                    or (prior.detail if prior and prior.status == STATUS_OK else ""),
                    part=tier,
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
    severity: str  # error | warn | info
    check: str
    message: str
    count: int = 0


@dataclass(frozen=True)
class QcReport:
    dataset: str
    days: int  # partitions on disk
    first: date | None
    last: date | None
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


def qc(root: Path, *, now: datetime | None = None, **_: Any) -> QcReport:
    """Integrity of the stored partitions and coverage of what is due."""
    day_store = store(root)
    parts = day_store.partitions_on_disk()
    state = day_store.load_state()
    findings: list[Finding] = []
    if not parts:
        return QcReport(
            SPEC.name,
            0,
            None,
            None,
            (Finding("info", "store", "no partitions on disk"),),
        )
    for week, tier in parts:
        key = partition_key(week, tier)
        frame = day_store.read_day(week, tier)
        if frame is None or list(frame.columns) != COLUMNS:
            findings.append(
                Finding(
                    "error", "schema", f"{key}: columns differ from the pinned schema"
                )
            )
            continue
        dup = int(
            frame.duplicated(
                subset=[
                    "summary_type",
                    "symbol",
                    "mpid",
                    "firm_crd",
                    "product_type",
                    "issue_name",
                ]
            ).sum()
        )
        if dup:
            findings.append(
                Finding("error", "duplicates", f"{key}: duplicate rows", dup)
            )
        off = int(
            (
                (frame["week_start"].astype(str) != week.isoformat())
                | (frame["tier"].astype(str) != tier)
            ).sum()
        )
        if off:
            findings.append(
                Finding(
                    "error", "partition", f"{key}: rows from another partition", off
                )
            )
        neg = int(((frame["trade_count"] < 0) | (frame["share_quantity"] < 0)).sum())
        if neg:
            findings.append(
                Finding("error", "negative", f"{key}: negative trades or shares", neg)
            )
        early = int((pd.to_datetime(frame["published_at"]) < pd.Timestamp(week)).sum())
        if early:
            findings.append(
                Finding(
                    "error",
                    "published_at",
                    f"{key}: published before the week started",
                    early,
                )
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
        if st.status == STATUS_OK and not day_store.day_path(st.day, st.part).exists():
            findings.append(
                Finding("error", "state", f"{key}: state ok but no file on disk")
            )
    errors = sorted(k for k, st in state.items() if st.status == STATUS_ERROR)
    if errors:
        findings.append(
            Finding(
                "warn",
                "errors",
                f"partitions in error: {', '.join(errors[-5:])}",
                len(errors),
            )
        )
    moment = now if now is not None else cal.now_et()
    on_disk = set(parts)
    gaps = [
        partition_key(w, t)
        for w in weeks(parts[0][0], parts[-1][0])
        for t in SPEC.parts
        if is_due(w, t, moment)
        and (w, t) not in on_disk
        and (
            state.get(partition_key(w, t)) is None
            or state[partition_key(w, t)].status == STATUS_ERROR
        )
    ]
    if gaps:
        findings.append(
            Finding(
                "warn",
                "gaps",
                f"due partitions neither stored nor absent: {', '.join(gaps[-5:])}",
                len(gaps),
            )
        )
    return QcReport(SPEC.name, len(parts), parts[0][0], parts[-1][0], tuple(findings))
