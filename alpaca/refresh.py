"""One auctions refresh: resolve the universe, plan, fetch, store, rebuild, assess coverage.

The orchestration depends only on interfaces: a :class:`SymbolUniverse`, an
:class:`AuctionStore`, an :class:`AuctionSource` (the client), a
:class:`StoreCloses` (the store's daily closes, for alias checks and
coverage), a :class:`QuietSchedule` and a clock. A dry run never builds a
client, so it needs no credentials and sends nothing.

Failure policy: a rejected key stops the run (every later request would fail
the same way); any other failed batch marks its units ``error`` with the
reason and the run carries on; an alias segment whose closing crosses do not
match the store's closes is refused (``error``, nothing stored); a quiet
window stops the run before the next request, with everything fetched so far
stored.

After the requests, prints left outside a ticker's current units (an older
segmentation) are pruned, the daily file is rebuilt and the per-ticker
coverage (:mod:`alpaca.coverage`) is written.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field, replace
from typing import Callable, Protocol, Sequence
from zoneinfo import ZoneInfo

import pandas as pd

from alpaca import auctions as au
from alpaca import coverage as cov
from alpaca.errors import AlpacaError, AuthError
from alpaca.plan import FIRST, Batch, FetchTask, batch_tasks, estimate_pages, plan_tasks
from alpaca.quiet import QuietSchedule
from alpaca.store import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_PARTIAL,
    AuctionStore,
    MergeResult,
    SymbolState,
    with_merge,
)
from alpaca.universe import ALIAS, SymbolSpan, SymbolUniverse, UniverseResult

log = logging.getLogger(__name__)
NEW_YORK = ZoneInfo("America/New_York")
#: after this New York time a day's auctions (and its late reports) are final enough to read
DAY_FINAL_AFTER = dt.time(20, 30)


class AuctionSource(Protocol):
    def auctions(
        self,
        symbols: Sequence[str],
        start: dt.date,
        end: dt.date,
        *,
        asof: dt.date | None = None,
    ) -> tuple[dict, int]: ...


class StoreCloses(Protocol):
    def closes(
        self, tickers: Sequence[str], start: dt.date, end: dt.date
    ) -> pd.DataFrame:
        """``ticker, date (datetime.date), close``: the store's daily closes."""
        ...


@dataclass(frozen=True)
class CoveragePolicy:
    min_coverage: float = 0.8
    max_gap_sessions: int = 20
    alias_tolerance: float = (
        0.002  # a closing cross within 0.2% of the store's close matches
    )
    alias_min_match: float = 0.9  # share of compared days an alias segment must match


def target_date(now: dt.datetime) -> dt.date:
    """The newest session to ask for: today in New York after 20:30, else the last weekday before."""
    local = now.astimezone(NEW_YORK)
    day = (
        local.date()
        if local.time() >= DAY_FINAL_AFTER
        else local.date() - dt.timedelta(days=1)
    )
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


@dataclass
class BatchOutcome:
    batch: Batch
    pages: int = 0
    days: int = 0
    prints: int = 0
    error: str = ""
    refused: list[str] = field(
        default_factory=list
    )  # alias units whose closes did not match


@dataclass
class RefreshReport:
    target: dt.date
    run: bool
    spans: int = 0  # fetch units
    tickers: int = 0
    skipped: dict[str, str] = field(default_factory=dict)
    tasks: list[FetchTask] = field(default_factory=list)
    batches: list[Batch] = field(default_factory=list)
    estimated_pages: int = 0
    outcomes: list[BatchOutcome] = field(default_factory=list)
    stopped: str = ""
    daily_rows: int | None = None
    pruned: int = 0
    coverage: pd.DataFrame | None = None

    @property
    def failed(self) -> list[BatchOutcome]:
        return [o for o in self.outcomes if o.error]

    @property
    def refused(self) -> list[str]:
        return [unit for o in self.outcomes for unit in o.refused]

    @property
    def requests(self) -> int:
        return sum(o.pages for o in self.outcomes)

    @property
    def in_sync(self) -> bool:
        return not self.tasks

    def thin(self) -> list[str]:
        if self.coverage is None or self.coverage.empty:
            return []
        return list(self.coverage.loc[self.coverage["status"] == cov.THIN, "ticker"])


def refresh(
    universe: SymbolUniverse,
    store: AuctionStore,
    *,
    target: dt.date,
    run: bool,
    overlap_days: int,
    per_request: int,
    page_limit: int,
    full: bool = False,
    client_factory: Callable[[], AuctionSource] | None = None,
    closes: StoreCloses | None = None,
    policy: CoveragePolicy = CoveragePolicy(),
    quiet: QuietSchedule | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.timezone.utc),
) -> RefreshReport:
    report = RefreshReport(target=target, run=run)
    resolved = universe.resolve(target)
    report.spans, report.skipped = len(resolved.spans), dict(resolved.skipped)
    report.tickers = len({s.ticker for s in resolved.spans})
    states = _with_spans(store.load_state(), resolved.spans)
    report.tasks = plan_tasks(
        resolved.spans, states, overlap_days=overlap_days, full=full
    )
    report.batches = batch_tasks(report.tasks, per_request=per_request)
    report.estimated_pages = sum(estimate_pages(b, page_limit) for b in report.batches)
    if not run:
        return report
    store.save_state(states)  # spans may have moved even when nothing is fetched
    changed = False
    if report.batches:
        if client_factory is None:
            raise ValueError("a run needs a client factory")
        client = client_factory()
        spans = {s.unit: s for s in resolved.spans}
        try:
            for batch in report.batches:
                if quiet is not None and (window := quiet.active(now())) is not None:
                    report.stopped = f"quiet window {window} {quiet.timezone}"
                    break
                outcome = _fetch_batch(
                    client, store, batch, states, spans, closes, policy
                )
                report.outcomes.append(outcome)
                changed = changed or not outcome.error
                store.save_state(states)
                store.append_audit([_audit_row(outcome)])
        finally:
            report.pruned = _prune(store, resolved, {t.ticker for t in report.tasks})
            if changed or report.pruned:
                report.daily_rows = store.rebuild_daily(au.select_crosses)
    if closes is not None and resolved.extents:
        report.coverage = assess_coverage(store, resolved, closes, policy)
    return report


def assess_coverage(
    store: AuctionStore,
    resolved: UniverseResult,
    closes: StoreCloses,
    policy: CoveragePolicy,
) -> pd.DataFrame:
    """Write and return the per-ticker coverage of the stored auctions."""
    start = min(s for s, _ in resolved.extents.values())
    end = max(e for _, e in resolved.extents.values())
    store_days = closes.closes(sorted(resolved.extents), start, end)
    frame = cov.assess(
        store.read_daily(),
        store_days,
        resolved.extents,
        min_coverage=policy.min_coverage,
        max_gap_sessions=policy.max_gap_sessions,
        gaps=resolved.gaps,
    )
    store.write_coverage(frame)
    return frame


def _with_spans(
    states: dict[str, SymbolState], spans: Sequence[SymbolSpan]
) -> dict[str, SymbolState]:
    """States carrying each unit's current target (``symbol`` and ``asof`` only change on a fetch)."""
    out = dict(states)
    for span in spans:
        unit = "" if span.unit == span.ticker else span.unit
        state = out.get(span.unit) or SymbolState(
            ticker=span.ticker,
            symbol=span.symbol,
            asof=span.asof,
            unit=unit,
            role=span.role,
        )
        out[span.unit] = replace(
            state, asset_type=span.asset_type, span_from=span.start, span_to=span.end
        )
    return out


def _fetch_batch(
    client: AuctionSource,
    store: AuctionStore,
    batch: Batch,
    states: dict[str, SymbolState],
    spans: dict[str, SymbolSpan],
    closes: StoreCloses | None,
    policy: CoveragePolicy,
) -> BatchOutcome:
    outcome = BatchOutcome(batch)
    stamp = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    try:
        records, outcome.pages = client.auctions(
            batch.symbols, batch.start, batch.end, asof=batch.asof
        )
        frame = au.parse_records(
            records, batch.tickers_by_symbol, batch.start, batch.end
        )
    except AuthError:
        raise
    except (AlpacaError, au.RecordError) as exc:
        outcome.error = str(exc)
        for task in batch.tasks:
            states[task.key] = replace(
                states[task.key],
                status=STATUS_ERROR,
                fetched_at=stamp,
                detail=outcome.error[:200],
            )
        log.warning(
            "auctions batch %s..%s failed: %s", batch.start, batch.end, outcome.error
        )
        return outcome
    outcome.days = int(frame[["ticker", "date"]].drop_duplicates().shape[0])
    outcome.prints = len(frame)
    for task in batch.tasks:
        part = frame[frame["symbol"] == task.symbol]
        if task.role == ALIAS and not part.empty:
            refusal = _alias_refusal(task, part, closes, policy)
            if refusal:
                outcome.refused.append(task.key)
                states[task.key] = replace(
                    states[task.key],
                    status=STATUS_ERROR,
                    fetched_at=stamp,
                    detail=refusal,
                )
                continue
        store.replace_range(task.ticker, part, task.start, task.end)
        span = spans[task.key]
        held = store.stats(task.ticker, span.start, span.end)
        states[task.key] = _covered(states[task.key], task, held, span, stamp)
    return outcome


def _alias_refusal(
    task: FetchTask,
    part: pd.DataFrame,
    closes: StoreCloses | None,
    policy: CoveragePolicy,
) -> str:
    """Why an alias segment must not be stored, or ``""``."""
    if closes is None:
        return "no store closes to check the alias against"
    store_closes = closes.closes([task.ticker], task.start, task.end)
    match = cov.verify_closes(part, store_closes, tolerance=policy.alias_tolerance)
    if match.compared == 0 or match.share < policy.alias_min_match:
        return (
            f"alias {task.symbol} closes match the store's {task.ticker} on "
            f"{match.matched} of {match.compared} days; not stored"
        )
    return ""


def _prune(store: AuctionStore, resolved: UniverseResult, tickers: set[str]) -> int:
    """Drop prints of ``tickers`` that lie outside every current unit."""
    ranges: dict[str, list[tuple[dt.date, dt.date]]] = {}
    for span in resolved.spans:
        ranges.setdefault(span.ticker, []).append((span.start, span.end))
    return sum(
        store.prune(ticker, ranges[ticker])
        for ticker in sorted(tickers)
        if ticker in ranges
    )


def _covered(
    state: SymbolState, task: FetchTask, held: MergeResult, span: SymbolSpan, stamp: str
) -> SymbolState:
    """The state after a successful task: coverage extended, counts of what the unit's span holds."""
    fresh = task.kind == FIRST
    coverage_from = (
        task.start
        if fresh or state.coverage_from is None
        else min(state.coverage_from, task.start)
    )
    coverage_through = (
        task.end
        if fresh or state.coverage_through is None
        else max(state.coverage_through, task.end)
    )
    updated = replace(
        with_merge(state, held),
        symbol=task.symbol,
        asof=task.asof,
        coverage_from=coverage_from,
        coverage_through=coverage_through,
        fetched_at=stamp,
        detail="",
    )
    if not updated.covers(span.start, span.end):
        status = STATUS_PARTIAL
    else:
        status = STATUS_OK if updated.n_prints else STATUS_EMPTY
    return replace(updated, status=status)


def _audit_row(outcome: BatchOutcome) -> dict:
    batch = outcome.batch
    return {
        "at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "start": batch.start.isoformat(),
        "end": batch.end.isoformat(),
        "asof": batch.asof.isoformat() if batch.asof else "",
        "symbols": len(batch.tasks),
        "pages": outcome.pages,
        "days": outcome.days,
        "prints": outcome.prints,
        "status": (
            "error" if outcome.error else ("refused" if outcome.refused else "ok")
        ),
        "detail": (outcome.error or ", ".join(outcome.refused))[:200],
    }
