"""Fetch planning: which date ranges each ticker still needs, grouped into requests.

Pure functions. A ticker's covered range is one interval (``coverage_from``
.. ``coverage_through``) and every task extends it at one end, so coverage
stays contiguous:

- ``first``: nothing stored yet (or the Alpaca symbol or its ``asof``
  changed, or ``--full``): the whole span;
- ``topup``: the span reaches past the coverage; the task starts
  ``overlap_days`` before the coverage end, so late official-close reports
  and vendor corrections of the last days are read again;
- ``backfill``: the span starts before the coverage.

Planning is per fetch unit (:attr:`SymbolSpan.unit`: a ticker's main
symbol, or one of its aliases). Tasks that share ``(start, end, asof)`` are
batched into one request of up to ``per_request`` symbols; two tasks with the
same Alpaca symbol never share a request (the answer is keyed by symbol).
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from alpaca.store import SymbolState
from alpaca.universe import SymbolSpan

FIRST, TOPUP, BACKFILL = "first", "topup", "backfill"


@dataclass(frozen=True)
class FetchTask:
    ticker: str
    symbol: str
    start: dt.date
    end: dt.date
    asof: dt.date | None
    kind: str
    unit: str = ""  # the state key; empty means the ticker's main unit
    role: str = "main"

    @property
    def key(self) -> str:
        return self.unit or self.ticker


@dataclass(frozen=True)
class Batch:
    start: dt.date
    end: dt.date
    asof: dt.date | None
    tasks: tuple[FetchTask, ...]

    @property
    def symbols(self) -> list[str]:
        return [t.symbol for t in self.tasks]

    @property
    def tickers_by_symbol(self) -> dict[str, str]:
        return {t.symbol: t.ticker for t in self.tasks}

    def task_for(self, symbol: str) -> FetchTask:
        return next(t for t in self.tasks if t.symbol == symbol)


def _task(span: SymbolSpan, start: dt.date, end: dt.date, kind: str) -> FetchTask:
    return FetchTask(
        span.ticker, span.symbol, start, end, span.asof, kind, span.unit, span.role
    )


def plan_tasks(
    spans: Iterable[SymbolSpan],
    states: Mapping[str, SymbolState],
    *,
    overlap_days: int,
    full: bool = False,
) -> list[FetchTask]:
    tasks: list[FetchTask] = []
    for span in spans:
        state = states.get(span.unit)

        fresh = (
            full
            or state is None
            or state.coverage_from is None
            or state.coverage_through is None
            or state.symbol != span.symbol
            or state.asof != span.asof
        )
        if fresh:
            tasks.append(_task(span, span.start, span.end, FIRST))
            continue
        assert state is not None and state.coverage_from and state.coverage_through
        if span.end > state.coverage_through:
            start = max(
                span.start, state.coverage_through - dt.timedelta(days=overlap_days)
            )
            tasks.append(_task(span, start, span.end, TOPUP))
        if span.start < state.coverage_from:
            end = state.coverage_from - dt.timedelta(days=1)
            tasks.append(_task(span, span.start, end, BACKFILL))
    return tasks


def batch_tasks(tasks: Sequence[FetchTask], *, per_request: int) -> list[Batch]:
    """Group by ``(start, end, asof)``, newest range first, ``per_request`` symbols at most."""
    if per_request < 1:
        raise ValueError("per_request must be >= 1")
    groups: dict[tuple[dt.date, dt.date, dt.date | None], list[FetchTask]] = {}
    for task in tasks:
        groups.setdefault((task.start, task.end, task.asof), []).append(task)
    batches: list[Batch] = []
    order = sorted(
        groups, key=lambda k: (k[1], k[0], k[2] or dt.date.max), reverse=True
    )
    for start, end, asof in order:
        pending = sorted(groups[(start, end, asof)], key=lambda t: t.ticker)
        while pending:
            chosen: list[FetchTask] = []
            seen: set[str] = set()
            later: list[FetchTask] = []
            for task in pending:
                if len(chosen) < per_request and task.symbol not in seen:
                    chosen.append(task)
                    seen.add(task.symbol)
                else:
                    later.append(task)
            batches.append(Batch(start, end, asof, tuple(chosen)))
            pending = later
    return batches


#: day records a page actually holds, whatever ``limit`` asks for: about 1,000
#: to 1,900 in the first backfill (2026-11-22), so 1,000 keeps the estimate an upper bound
OBSERVED_DAYS_PER_PAGE = 1_000


def estimate_pages(batch: Batch, page_limit: int) -> int:
    """Pages a batch needs at most: one day record per symbol and weekday."""
    weekdays = sum(
        1
        for offset in range((batch.end - batch.start).days + 1)
        if (batch.start + dt.timedelta(days=offset)).weekday() < 5
    )
    per_page = min(page_limit, OBSERVED_DAYS_PER_PAGE)
    return max(1, math.ceil(len(batch.tasks) * weekdays / per_page))
