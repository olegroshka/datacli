"""Reg SHO daily short sale volume: the first FINRA dataset.

One row per ``(date, symbol)`` for exchange-listed (NMS) securities, holding
the consolidated short, short-exempt and total volume FINRA publishes, plus
the facility codes that reported it. Stored raw: the SIP symbol spelling
(``BRK/B``, ``ABRpD``) and the volumes exactly as published, whole shares
until early 2026 and fractional shares since, hence float64. Mapping to
EODHD tickers happens in the view (:func:`eodhd_code`), not at ingest.

Two transports normalise into the same frame:

- :class:`CdnTransport` (default): the public consolidated file, one row per
  symbol, full history since 2018-08-01;
- :class:`ApiTransport`: the Query API's per-facility rows, summed per symbol
  (the API serves a rolling one-year window).

A day is stored from one transport only; the ``source`` column says which.

:func:`refresh` is the fetch loop: plan the days (newest first), fetch each,
write it and its state row before moving on (an interrupted backfill resumes),
and apply two guards from the design doc: "absent" is re-probed while inside
the overlap window (late publication), and a third consecutive absent weekday
aborts the run as a suspected outage or block.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

import pandas as pd
import pyarrow as pa

from finra import calendar as cal
from finra import registry as reg
from finra.api import Filters, QueryApiClient
from finra.cdn import DailyFile, DailyFileClient, normalise_facilities
from finra.errors import FinraError
from finra.store import (
    STATUS_ABSENT,
    STATUS_ERROR,
    STATUS_OK,
    DayState,
    DayStore,
)

SPEC = reg.spec("short_volume")
SOURCE_CDN = "cdn"
SOURCE_API = "api"
SOURCES = (SOURCE_CDN, SOURCE_API)

COLUMNS = [
    "date",
    "symbol",
    "short_volume",
    "short_exempt_volume",
    "total_volume",
    "facilities",
    "source",
]
SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("symbol", pa.string()),
        ("short_volume", pa.float64()),
        ("short_exempt_volume", pa.float64()),
        ("total_volume", pa.float64()),
        ("facilities", pa.string()),
        ("source", pa.string()),
    ]
)

DEFAULT_OVERLAP_DAYS = 3  # weekdays re-checked for restatements and late files
MAX_ABSENT_STREAK = 2  # US markets have not closed 3 consecutive weekdays since 2012

# API field names (verified against the metadata endpoint, 2026-10-01)
API_DATE = "tradeReportDate"
API_SYMBOL = "securitiesInformationProcessorSymbolIdentifier"
API_SHORT = "shortParQuantity"
API_EXEMPT = "shortExemptParQuantity"
API_TOTAL = "totalParQuantity"
API_MARKET = "marketCode"


def store(root: Path) -> DayStore:
    return DayStore(root, SPEC.name, SCHEMA)


# --------------------------------------------------------------------------- #
# symbol mapping (dataset knowledge; the DuckDB view mirrors it in SQL)
# --------------------------------------------------------------------------- #
_PREFERRED = re.compile(r"^[A-Z]+p[A-Z]?$")  # ABRpD, ACPpA: lower-case p + series
_SUFFIX_KINDS = {"/WS": "warrant", "/U": "unit", "/R": "right"}


def security_kind(symbol: str) -> str:
    """``common`` | ``preferred`` | ``warrant`` | ``unit`` | ``right`` | ``other`` from the SIP spelling."""
    for suffix, kind in _SUFFIX_KINDS.items():
        if symbol.endswith(suffix):
            return kind
    if _PREFERRED.match(symbol):
        return "preferred"
    if re.fullmatch(r"[A-Z]+(/[A-Z])?", symbol):
        return "common"  # plain, or a share class: BRK/B
    return "other"


def eodhd_code(symbol: str) -> str | None:
    """The EODHD ``Code`` for a common symbol (``BRK/B`` -> ``BRK-B``), else ``None``."""
    if security_kind(symbol) != "common":
        return None
    return symbol.replace("/", "-")


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def frame_from_daily_file(daily: DailyFile) -> pd.DataFrame:
    rows = [
        {
            "date": daily.trade_date,
            "symbol": r.symbol,
            "short_volume": r.short_volume,
            "short_exempt_volume": r.short_exempt_volume,
            "total_volume": r.total_volume,
            "facilities": r.facilities,
            "source": SOURCE_CDN,
        }
        for r in daily.rows
    ]
    return _finish(pd.DataFrame(rows, columns=COLUMNS))


def frame_from_api_rows(
    trade_date: date, rows: Sequence[Mapping[str, Any]]
) -> pd.DataFrame:
    """Sum the API's per-facility rows per symbol (shares, fractional allowed)."""
    if not rows:
        return _finish(pd.DataFrame(columns=COLUMNS))
    frame = pd.DataFrame(list(rows))
    needed = [API_DATE, API_SYMBOL, API_SHORT, API_EXEMPT, API_TOTAL, API_MARKET]
    missing = [c for c in needed if c not in frame.columns]
    if missing:
        raise ValueError(f"API rows lack fields {missing}")
    wrong = frame[frame[API_DATE].astype(str) != trade_date.isoformat()]
    if not wrong.empty:
        raise ValueError(
            f"API rows carry {wrong[API_DATE].iloc[0]!r}, expected {trade_date}"
        )
    grouped = (
        frame.groupby(API_SYMBOL, sort=True)
        .agg(
            short_volume=(API_SHORT, "sum"),
            short_exempt_volume=(API_EXEMPT, "sum"),
            total_volume=(API_TOTAL, "sum"),
            facilities=(
                API_MARKET,
                lambda s: normalise_facilities(",".join(map(str, s))),
            ),
        )
        .reset_index()
        .rename(columns={API_SYMBOL: "symbol"})
    )
    for col in ("short_volume", "short_exempt_volume", "total_volume"):
        grouped[col] = grouped[col].astype("float64")
    grouped["date"] = trade_date
    grouped["source"] = SOURCE_API
    return _finish(grouped[COLUMNS])


def _finish(frame: pd.DataFrame) -> pd.DataFrame:
    """Canonical dtypes and order; the invariants the parser already enforced, re-checked."""
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"]).dt.date
    for col in ("short_volume", "short_exempt_volume", "total_volume"):
        out[col] = out[col].astype("float64")
    out["symbol"] = out["symbol"].astype(str)
    out["facilities"] = out["facilities"].astype(str)
    out["source"] = out["source"].astype(str)
    bad = out[(out["short_volume"] > out["total_volume"]) | (out["short_volume"] < 0)]
    if not bad.empty:
        raise ValueError(f"{len(bad)} rows with short > total or negative volume")
    if out["symbol"].duplicated().any():
        raise ValueError("duplicate symbols in one day")
    return out.sort_values("symbol").reset_index(drop=True)[COLUMNS]


def frame_digest(frame: pd.DataFrame) -> str:
    """A content digest of the canonical rows (same rows -> same digest, any source)."""
    payload = (
        frame[COLUMNS[:-1]].to_csv(index=False, lineterminator="\n").encode("utf-8")
    )
    return hashlib.sha256(payload).hexdigest()


# --------------------------------------------------------------------------- #
# transports
# --------------------------------------------------------------------------- #
class Transport(Protocol):
    name: str

    def fetch_day(self, trade_date: date) -> pd.DataFrame | None:
        """The day's canonical frame, or ``None`` when FINRA has not published it."""


class CdnTransport:
    name = SOURCE_CDN

    def __init__(
        self, client: DailyFileClient, family: str = SPEC.cdn_family or "CNMS"
    ) -> None:
        self._client = client
        self._family = family

    def fetch_day(self, trade_date: date) -> pd.DataFrame | None:
        daily = self._client.fetch_day(self._family, trade_date)
        return None if daily is None else frame_from_daily_file(daily)


class ApiTransport:
    name = SOURCE_API

    def __init__(self, client: QueryApiClient) -> None:
        self._client = client

    def fetch_day(self, trade_date: date) -> pd.DataFrame | None:
        rows = self._client.query(
            SPEC.api_group,
            SPEC.api_name,
            filters=Filters.equal(SPEC.partition_field, trade_date.isoformat()),
        )
        return None if not rows else frame_from_api_rows(trade_date, rows)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def plan_days(
    *,
    from_date: date,
    to_date: date,
    state: Mapping[str, DayState],
    now: datetime,
    overlap_days: int = DEFAULT_OVERLAP_DAYS,
    retry_absent: bool = False,
    full_refresh: bool = False,
) -> list[date]:
    """Trade dates to fetch this run, **newest first**.

    A weekday in ``[from_date, to_date]`` that FINRA is due to have published
    is planned when it has no ``ok`` row, or is ``error``, or lies inside the
    trailing ``overlap_days`` weekdays (``ok`` days are re-checked for
    restatements there, ``absent`` days for late publication), or is
    ``absent`` and ``retry_absent`` is set. ``full_refresh`` plans every day.
    """
    end = min(to_date, cal.latest_publishable(now))
    if end < from_date:
        return []
    # the overlap window is the last ``overlap_days`` weekdays ending at ``end``
    # (inclusive); 0 means no re-check at all
    overlap_from = (
        cal.weekdays_back(end, overlap_days - 1) if overlap_days > 0 else None
    )
    planned: list[date] = []
    for day in cal.weekdays(from_date, end):
        prior = state.get(day.isoformat())
        if full_refresh or prior is None or prior.status == STATUS_ERROR:
            planned.append(day)
        elif overlap_from is not None and day >= overlap_from:
            planned.append(day)
        elif prior.status == STATUS_ABSENT and retry_absent:
            planned.append(day)
    planned.reverse()
    return planned


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RefreshReport:
    """What one ``refresh`` call planned and, with ``run``, did."""

    dataset: str
    run: bool
    source: str
    root: Path
    planned: tuple[date, ...]
    stored: tuple[date, ...] = ()
    restated: tuple[date, ...] = ()
    absent: tuple[date, ...] = ()
    failed: tuple[tuple[date, str], ...] = ()
    rows: int = 0
    aborted: str = ""  # reason, when the absent-streak guard stopped the run
    not_yet_publishable: date | None = None  # today, when it was asked for too early
    skipped: tuple[date, ...] = field(
        default_factory=tuple
    )  # left unplanned by --limit-days

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
    """Plan (and with ``run``) fetch short volume days into ``root``.

    Dry run returns the plan and touches nothing. With ``run`` each day is
    fetched, written and recorded in the state sidecar before the next, so a
    killed backfill resumes where it stopped.
    """
    moment = now if now is not None else cal.now_et()
    start = from_date or SPEC.first_date
    asked_end = to_date or cal.to_et(moment).date()
    end = cal.latest_publishable(moment)
    not_yet = asked_end if asked_end > end else None
    day_store = store(root)
    state = day_store.load_state()
    planned = plan_days(
        from_date=start,
        to_date=asked_end,
        state=state,
        now=moment,
        overlap_days=overlap_days,
        retry_absent=retry_absent,
        full_refresh=full_refresh,
    )
    skipped: tuple[date, ...] = ()
    if limit_days is not None and limit_days >= 0 and len(planned) > limit_days:
        planned, skipped = planned[:limit_days], tuple(planned[limit_days:])
    base = dict(
        dataset=SPEC.name,
        run=run,
        source=transport.name,
        root=Path(root),
        planned=tuple(planned),
        not_yet_publishable=not_yet,
        skipped=skipped,
    )
    if not run:
        return RefreshReport(**base)  # type: ignore[arg-type]

    stored: list[date] = []
    restated: list[date] = []
    absent: list[date] = []
    failed: list[tuple[date, str]] = []
    rows = 0
    aborted = ""
    streak: list[date] = []  # consecutive absent weekdays, newest first
    say = progress or (lambda _m: None)

    for day in planned:
        prior = state.get(day.isoformat())
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            frame = transport.fetch_day(day)
        except (FinraError, ValueError) as exc:
            failed.append((day, f"{type(exc).__name__}: {exc}"))
            day_store.upsert_state(
                [
                    DayState(
                        day.isoformat(),
                        STATUS_ERROR,
                        transport.name,
                        fetched_at=stamp,
                        detail=str(exc)[:300],
                    )
                ]
            )
            say(f"{day}  error  {type(exc).__name__}: {exc}")
            if log:
                log.warning("%s: %s: %s", day, type(exc).__name__, exc)
            continue
        except Exception as exc:  # a connection error after retries, a parse crash
            failed.append((day, f"{type(exc).__name__}: {exc}"))
            day_store.upsert_state(
                [
                    DayState(
                        day.isoformat(),
                        STATUS_ERROR,
                        transport.name,
                        fetched_at=stamp,
                        detail=f"{type(exc).__name__}: {exc}"[:300],
                    )
                ]
            )
            say(f"{day}  error  {type(exc).__name__}: {exc}")
            if log:
                log.warning("%s: %s: %s", day, type(exc).__name__, exc)
            continue

        if frame is None:
            absent.append(day)
            if streak and cal.previous_weekday(streak[-1]) == day:
                streak.append(day)
            else:
                streak = [day]
            detail = (
                "no file published"
                if prior is None or prior.status != STATUS_OK
                else "file vanished"
            )
            day_store.upsert_state(
                [
                    DayState(
                        day.isoformat(),
                        STATUS_ABSENT,
                        transport.name,
                        fetched_at=stamp,
                        detail=detail,
                    )
                ]
            )
            say(f"{day}  absent")
            if len(streak) > MAX_ABSENT_STREAK:
                aborted = (
                    f"{len(streak)} consecutive weekdays absent ({streak[-1]}..{streak[0]}): "
                    "suspect outage or block, not holidays"
                )
                day_store.upsert_state(
                    [
                        DayState(
                            d.isoformat(),
                            STATUS_ERROR,
                            transport.name,
                            fetched_at=stamp,
                            detail="absent streak: " + aborted,
                        )
                        for d in streak
                    ]
                )
                for d in streak:
                    absent.remove(d)
                    failed.append((d, "absent streak"))
                if log:
                    log.error("aborting: %s", aborted)
                say(f"abort  {aborted}")
                break
            continue

        streak = []
        digest = frame_digest(frame)
        detail = ""
        if (
            prior is not None
            and prior.status == STATUS_OK
            and prior.sha256
            and prior.sha256 != digest
        ):
            restated.append(day)
            detail = (
                f"restated {stamp[:10]}: previously rows={prior.rows} "
                f"short_sum={prior.short_sum} total_sum={prior.total_sum} sha256={prior.sha256[:12]}"
            )
        day_store.write_day(day, frame)
        day_store.upsert_state(
            [
                DayState(
                    day.isoformat(),
                    STATUS_OK,
                    transport.name,
                    rows=int(len(frame)),
                    short_sum=int(round(frame["short_volume"].sum())),
                    total_sum=int(round(frame["total_volume"].sum())),
                    sha256=digest,
                    fetched_at=stamp,
                    detail=detail
                    or (prior.detail if prior and prior.status == STATUS_OK else ""),
                )
            ]
        )
        stored.append(day)
        rows += int(len(frame))
        say(
            f"{day}  ok  {len(frame):,} rows"
            + ("  (restated)" if detail.startswith("restated") else "")
        )
        if log:
            log.info("%s: %d rows%s", day, len(frame), " (restated)" if detail else "")

    return RefreshReport(
        **base,  # type: ignore[arg-type]
        stored=tuple(stored),
        restated=tuple(restated),
        absent=tuple(absent),
        failed=tuple(failed),
        rows=rows,
        aborted=aborted,
    )


# --------------------------------------------------------------------------- #
# quality checks over the store
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
    days: int
    first: date | None
    last: date | None
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "error" for f in self.findings)


def qc(root: Path, *, eodhd_codes: Sequence[str] | None = None) -> QcReport:
    """Integrity of the stored days plus coverage against the state sidecar.

    ``eodhd_codes`` (EODHD ``Code`` values of the common-stock universe) is
    optional; when given, the share of codes with no FINRA row on the latest
    day is reported.
    """
    day_store = store(root)
    days = day_store.days_on_disk()
    state = day_store.load_state()
    findings: list[Finding] = []
    if not days:
        return QcReport(
            SPEC.name, 0, None, None, (Finding("info", "store", "no days on disk"),)
        )

    sources: set[str] = set()
    for day in days:
        frame = day_store.read_day(day)
        if frame is None or list(frame.columns) != COLUMNS:
            findings.append(
                Finding(
                    "error", "schema", f"{day}: columns differ from the pinned schema"
                )
            )
            continue
        sources.update(frame["source"].astype(str).unique())
        dup = int(frame["symbol"].duplicated().sum())
        if dup:
            findings.append(
                Finding("error", "duplicates", f"{day}: duplicate symbols", dup)
            )
        bad_date = int((pd.to_datetime(frame["date"]).dt.date != day).sum())
        if bad_date:
            findings.append(
                Finding(
                    "error",
                    "date",
                    f"{day}: rows dated differently from the file",
                    bad_date,
                )
            )
        over = int((frame["short_volume"] > frame["total_volume"]).sum())
        if over:
            findings.append(
                Finding("error", "short_gt_total", f"{day}: short > total", over)
            )
        exempt = int((frame["short_exempt_volume"] > frame["short_volume"]).sum())
        if exempt:
            findings.append(
                Finding("error", "exempt_gt_short", f"{day}: exempt > short", exempt)
            )
        neg = int(
            (frame[["short_volume", "short_exempt_volume", "total_volume"]] < 0)
            .any(axis=1)
            .sum()
        )
        if neg:
            findings.append(
                Finding("error", "negative", f"{day}: negative volumes", neg)
            )
        prior = state.get(day.isoformat())
        if prior is None or prior.status != STATUS_OK:
            findings.append(
                Finding(
                    "warn",
                    "state",
                    f"{day}: file on disk but state is {prior.status if prior else 'missing'}",
                )
            )
        elif prior.rows != len(frame):
            findings.append(
                Finding(
                    "warn",
                    "state",
                    f"{day}: state says {prior.rows} rows, file holds {len(frame)}",
                )
            )

    for iso, st in state.items():
        if st.status == STATUS_OK and not day_store.day_path(st.day).exists():
            findings.append(
                Finding("error", "state", f"{iso}: state ok but no file on disk")
            )
    errors = [iso for iso, st in state.items() if st.status == STATUS_ERROR]
    if errors:
        findings.append(
            Finding(
                "warn",
                "errors",
                f"days in error: {', '.join(sorted(errors)[-5:])}",
                len(errors),
            )
        )
    stored = set(days)
    gaps = [
        d
        for d in cal.weekdays(days[0], days[-1])
        if d not in stored
        and (
            state.get(d.isoformat()) is None
            or state[d.isoformat()].status == STATUS_ERROR
        )
    ]
    if gaps:
        findings.append(
            Finding(
                "warn",
                "gaps",
                f"weekdays neither stored nor absent: {', '.join(d.isoformat() for d in gaps[-5:])}",
                len(gaps),
            )
        )
    if len(sources) > 1:
        findings.append(
            Finding(
                "info",
                "sources",
                f"days come from mixed sources: {', '.join(sorted(sources))}",
            )
        )

    if eodhd_codes is not None:
        latest = day_store.read_day(days[-1])
        if latest is not None:
            have = {eodhd_code(s) for s in latest["symbol"].astype(str)} - {None}
            universe = set(eodhd_codes)
            unmatched = sorted(universe - have)
            share = len(unmatched) / len(universe) if universe else 0.0
            findings.append(
                Finding(
                    "warn" if share > 0.05 else "info",
                    "eodhd_coverage",
                    f"{days[-1]}: {len(unmatched)} of {len(universe)} EODHD common codes have no FINRA row ({share:.1%})"
                    + (f"; e.g. {', '.join(unmatched[:5])}" if unmatched else ""),
                    len(unmatched),
                )
            )
    return QcReport(SPEC.name, len(days), days[0], days[-1], tuple(findings))
