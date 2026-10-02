"""Consolidated short interest: the third FINRA dataset (entitled, API-only).

Twice a month, on a mid-month and a month-end settlement date, FINRA
consolidates the short positions every member firm reports for every
symbol: the position, the change from the previous report, the average
daily volume and days to cover. One row per ``(settlement_date, symbol)``,
stored as published for every market class (the whole dataset is one API
partition per settlement date and under five million rows, so filtering OTC
at ingest would save nothing; the views expose the exchange-listed classes).

Point in time: FINRA publishes a settlement date's positions seven business
days later (positions are due two business days after settlement and are
disseminated five business days after that; checked against FINRA's
schedule, holidays included). The API carries no publication date, so
``published_at`` is set at ingest from that rule with the NYSE calendar in
:mod:`finra.calendar`. Join on ``published_at``, never on the settlement
date.

Symbols here carry no separators (``BRKB``, preferreds as ``ABRPRD``); the
mapping to EODHD tickers goes through the SIP spellings in the daily short
volume store and lives in the views.
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
from finra.api import Filters, QueryApiClient
from finra.store import (
    STATUS_ABSENT,
    STATUS_ERROR,
    STATUS_OK,
    DayState,
    DayStore,
    partition_key,
)

SPEC = reg.spec("short_interest")
SOURCE_API = "api"
PUBLICATION_LAG_BUSINESS_DAYS = 7
DEFAULT_OVERLAP_DAYS = 21  # settlement dates published this recently are re-checked

COLUMNS = [
    "settlement_date",
    "symbol",
    "issue_name",
    "market_class",
    "exchange_code",
    "short_position",
    "previous_short_position",
    "change_shares",
    "change_percent",
    "average_daily_volume",
    "days_to_cover",
    "revised",
    "split_adjusted",
    "published_at",
    "source",
]
SCHEMA = pa.schema(
    [
        ("settlement_date", pa.date32()),
        ("symbol", pa.string()),
        ("issue_name", pa.string()),
        ("market_class", pa.string()),
        ("exchange_code", pa.string()),
        ("short_position", pa.int64()),
        ("previous_short_position", pa.int64()),
        ("change_shares", pa.int64()),
        ("change_percent", pa.float64()),
        ("average_daily_volume", pa.int64()),
        ("days_to_cover", pa.float64()),
        ("revised", pa.bool_()),
        ("split_adjusted", pa.bool_()),
        ("published_at", pa.date32()),
        ("source", pa.string()),
    ]
)

# API field names (verified against the metadata endpoint, 2026-10-02)
API_FIELDS = {
    "settlementDate": "settlement_date",
    "symbolCode": "symbol",
    "issueName": "issue_name",
    "marketClassCode": "market_class",
    "issuerServicesGroupExchangeCode": "exchange_code",
    "currentShortPositionQuantity": "short_position",
    "previousShortPositionQuantity": "previous_short_position",
    "changePreviousNumber": "change_shares",
    "changePercent": "change_percent",
    "averageDailyVolumeQuantity": "average_daily_volume",
    "daysToCoverQuantity": "days_to_cover",
    "revisionFlag": "revised",
    "stockSplitFlag": "split_adjusted",
}
#: Market classes that are exchange-listed (NMS); the rest is OTC.
LISTED_CLASSES = ("NYSE", "NNM", "ARCA", "BZX", "AMEX", "SC")


def store(root: Path) -> DayStore:
    return DayStore(root, SPEC.name, SCHEMA, subdir=SPEC.subdir)


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def _on_or_before(day: date) -> date:
    cursor = day
    while not cal.is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def settlement_dates(start: date, end: date) -> list[date]:
    """FINRA's settlement dates in ``[start, end]``: the 15th and the last day
    of each month, each moved back to the preceding trading day when needed."""
    out: list[date] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        mid = _on_or_before(date(year, month, 15))
        last_of_month = date(year + (month == 12), month % 12 + 1, 1) - timedelta(
            days=1
        )
        for day in (mid, _on_or_before(last_of_month)):
            if start <= day <= end:
                out.append(day)
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def published_on(settlement_date: date) -> date:
    return cal.business_days_after(settlement_date, PUBLICATION_LAG_BUSINESS_DAYS)


def is_due(settlement_date: date, now: datetime) -> bool:
    return cal.to_et(now).date() >= published_on(settlement_date)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def frame_from_api_rows(
    settlement_date: date, rows: Sequence[Mapping[str, Any]]
) -> pd.DataFrame:
    if not rows:
        return _finish(pd.DataFrame(columns=COLUMNS))
    frame = pd.DataFrame(list(rows))
    missing = [c for c in API_FIELDS if c not in frame.columns]
    if missing:
        raise ValueError(f"API rows lack fields {missing}")
    frame = frame[list(API_FIELDS)].rename(columns=API_FIELDS)
    wrong = frame[frame["settlement_date"].astype(str) != settlement_date.isoformat()]
    if not wrong.empty:
        raise ValueError(
            f"API rows carry settlement {wrong['settlement_date'].iloc[0]!r}, "
            f"expected {settlement_date}"
        )
    frame["published_at"] = published_on(settlement_date)
    frame["source"] = SOURCE_API
    return _finish(frame)


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    for col in ("settlement_date", "published_at"):
        out[col] = pd.to_datetime(out[col], errors="coerce").dt.date
    for col in ("symbol", "issue_name", "market_class", "exchange_code", "source"):
        out[col] = out[col].astype("string")
    for col in (
        "short_position",
        "previous_short_position",
        "change_shares",
        "average_daily_volume",
    ):
        out[col] = pd.to_numeric(out[col], errors="coerce").fillna(0).astype("int64")
    for col in ("change_percent", "days_to_cover"):
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    out["revised"] = out["revised"].astype("string").fillna("").str.upper().eq("R")
    out["split_adjusted"] = (
        out["split_adjusted"].astype("string").fillna("").str.upper().eq("S")
    )
    if (
        out["symbol"].isna().any()
        or (out["symbol"].astype(str).str.strip() == "").any()
    ):
        raise ValueError("rows with an empty symbol")
    if out.duplicated(subset=["symbol"]).any():
        raise ValueError("duplicate symbols in one settlement date")
    bad = out[(out["short_position"] < 0) | (out["average_daily_volume"] < 0)]
    if not bad.empty:
        raise ValueError(f"{len(bad)} rows with a negative position or volume")
    return out.sort_values("symbol").reset_index(drop=True)[COLUMNS]


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

    def fetch_partition(self, settlement_date: date) -> pd.DataFrame | None:
        """The settlement date's canonical frame, or ``None`` when FINRA has no rows."""


class ApiTransport:
    name = SOURCE_API

    def __init__(self, client: QueryApiClient) -> None:
        self._client = client

    def fetch_partition(self, settlement_date: date) -> pd.DataFrame | None:
        rows = self._client.query(
            SPEC.api_group,
            SPEC.api_name,
            filters=Filters.equal(SPEC.partition_field, settlement_date.isoformat()),
        )
        return None if not rows else frame_from_api_rows(settlement_date, rows)


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
    """Settlement dates to fetch, newest first.

    A settlement date is planned when it is due (its publication day has
    passed) and either has no ``ok`` row, or is ``error``, or was published
    inside the trailing ``overlap_days`` (revisions), or is ``absent`` with
    ``retry_absent``.
    """
    today = cal.to_et(now).date()
    planned: list[date] = []
    for day in settlement_dates(from_date, to_date):
        if not is_due(day, now):
            continue
        prior = state.get(partition_key(day))
        recent = overlap_days > 0 and published_on(day) >= today - timedelta(
            days=overlap_days
        )
        if full_refresh or prior is None or prior.status == STATUS_ERROR:
            planned.append(day)
        elif recent:
            planned.append(day)
        elif prior.status == STATUS_ABSENT and retry_absent:
            planned.append(day)
    planned.sort(reverse=True)
    return planned


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #
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
    """Plan (and with ``run``) fetch settlement dates into ``root``; ``limit_days`` caps partitions."""
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
    keys = [partition_key(d) for d in planned]
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
    for day, key in zip(planned, keys):
        prior = state.get(key)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            frame = transport.fetch_partition(day)
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
                        detail="no rows published",
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
            revised = int(frame["revised"].sum())
            detail = f"restated {stamp[:10]}: previously rows={prior.rows} sha256={prior.sha256[:12]}; {revised} rows flagged R"
        day_store.write_day(day, frame)
        day_store.upsert_state(
            [
                DayState(
                    key,
                    STATUS_OK,
                    transport.name,
                    rows=int(len(frame)),
                    short_sum=int(round(frame["short_position"].sum())),
                    total_sum=int(round(frame["average_daily_volume"].sum())),
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
    """Integrity of the stored settlement dates and coverage of what is due."""
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
            (Finding("info", "store", "no settlement dates on disk"),),
        )
    for day in days:
        key = partition_key(day)
        frame = day_store.read_day(day)
        if frame is None or list(frame.columns) != COLUMNS:
            findings.append(
                Finding(
                    "error", "schema", f"{key}: columns differ from the pinned schema"
                )
            )
            continue
        dup = int(frame["symbol"].duplicated().sum())
        if dup:
            findings.append(
                Finding("error", "duplicates", f"{key}: duplicate symbols", dup)
            )
        off = int((frame["settlement_date"].astype(str) != day.isoformat()).sum())
        if off:
            findings.append(
                Finding(
                    "error",
                    "partition",
                    f"{key}: rows from another settlement date",
                    off,
                )
            )
        wrong_pub = int(
            (pd.to_datetime(frame["published_at"]).dt.date != published_on(day)).sum()
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
        neg = int(
            ((frame["short_position"] < 0) | (frame["average_daily_volume"] < 0)).sum()
        )
        if neg:
            findings.append(
                Finding(
                    "error", "negative", f"{key}: negative positions or volume", neg
                )
            )
        if day not in settlement_dates(day, day):
            findings.append(
                Finding("warn", "calendar", f"{key}: not a settlement date by the rule")
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
                f"settlement dates in error: {', '.join(errors[-5:])}",
                len(errors),
            )
        )
    moment = now if now is not None else cal.now_et()
    on_disk = set(days)
    gaps = [
        d.isoformat()
        for d in settlement_dates(days[0], days[-1])
        if is_due(d, moment)
        and d not in on_disk
        and (
            state.get(partition_key(d)) is None
            or state[partition_key(d)].status == STATUS_ERROR
        )
    ]
    if gaps:
        findings.append(
            Finding(
                "warn",
                "gaps",
                f"due settlement dates neither stored nor absent: {', '.join(gaps[-5:])}",
                len(gaps),
            )
        )
    return QcReport(SPEC.name, len(days), days[0], days[-1], tuple(findings))
