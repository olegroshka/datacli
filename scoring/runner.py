"""Plan / run / status for a scoring job.

A job = (schema, backend, target selection). ``plan`` counts pending items per
day and prints the estimate; ``run`` scores day by day (newest first), writing
the partition and the state row after every chunk so a killed run resumes where
it stopped; ``status`` summarises what exists on disk. All I/O goes through
``scoring.store``; all reads of the corpus through ``scoring.select``.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from scoring import select as sel
from scoring import store
from scoring.backends.base import Backend, Item, Result
from scoring.schema import Schema

log = logging.getLogger("scoring")


@dataclass
class Target:
    """The selection part of a job."""

    since: str
    until: str
    use_universe: bool = True
    max_symbols: int = 3
    sample_per_day: int | None = None
    seed: int = 0
    limit: int | None = None  # total items this run (smoke tests / budgeted passes)
    force: bool = False  # re-score already-ok items


@dataclass
class DayPlan:
    day: str
    n_candidates: int
    n_pending: int
    seconds: float
    cost_usd: float


@dataclass
class Plan:
    schema: str
    backend: str
    model: str
    target: Target
    days: list[DayPlan] = field(default_factory=list)
    note: str = ""

    @property
    def n_pending(self) -> int:
        return sum(d.n_pending for d in self.days)

    @property
    def n_candidates(self) -> int:
        return sum(d.n_candidates for d in self.days)

    @property
    def seconds(self) -> float:
        return sum(d.seconds for d in self.days)

    @property
    def cost_usd(self) -> float:
        return sum(d.cost_usd for d in self.days)


def default_window(days: int, today: date | None = None) -> tuple[str, str]:
    """``(since, until)`` = the last ``days`` days up to today (UTC)."""
    end = today or date.today()
    start = end - timedelta(days=max(days - 1, 0))
    return start.isoformat(), end.isoformat()


def _prepare_universe(con: Any, target: Target) -> None:
    if target.use_universe:
        sel.install_universe(con, sel.universe_symbols(con))


def _pending_for_day(
    con: Any, day: str, target: Target, directory: Path
) -> tuple[list[Item], int]:
    """Items to score for one day and the pre-exclusion candidate count."""
    excl = (
        set()
        if target.force
        else store.scored_ids(store.partition_path(directory, day))
    )
    all_items = sel.select_day(
        con,
        day,
        use_universe=target.use_universe,
        max_symbols=target.max_symbols,
        sample=target.sample_per_day,
        seed=target.seed,
    )
    pending = [it for it in all_items if it.article_id not in excl]
    return pending, len(all_items)


def plan(
    con: Any,
    *,
    data_root: Path,
    schema: Schema,
    backend: Backend,
    target: Target,
) -> Plan:
    """Count pending items per day and estimate the run. Never scores."""
    _prepare_universe(con, target)
    directory = store.sidecar_dir(data_root, schema, backend.id, backend.kind)
    out = Plan(
        schema=schema.key, backend=backend.id, model=backend.model, target=target
    )
    remaining = target.limit
    for day in sel.days_with_articles(con, target.since, target.until):
        pending, n_all = _pending_for_day(con, day, target, directory)
        if remaining is not None:
            pending = pending[:remaining]
            remaining -= len(pending)
        est = backend.estimate(pending, schema)
        out.days.append(DayPlan(day, n_all, len(pending), est.seconds, est.cost_usd))
        out.note = est.note
        if remaining is not None and remaining <= 0:
            break
    return out


def run(
    con: Any,
    *,
    data_root: Path,
    schema: Schema,
    backend: Backend,
    target: Target,
    chunk: int = 25,
    on_progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Score the pending items day by day, resumably. Returns run totals."""
    _prepare_universe(con, target)
    directory = store.sidecar_dir(data_root, schema, backend.id, backend.kind)
    directory.mkdir(parents=True, exist_ok=True)
    say = on_progress or (lambda msg: log.info(msg))
    totals = {
        "days": 0,
        "n_ok": 0,
        "n_invalid": 0,
        "n_error": 0,
        "n_skipped": 0,
        "seconds": 0.0,
        "cost_usd": 0.0,
        "stopped": "",
    }
    remaining = target.limit
    state = store.read_state(directory)
    is_vector = backend.kind == "vector"
    arrow = (
        store.vector_arrow_schema() if is_vector else store.score_arrow_schema(schema)
    )
    keys = ["article_id"] if is_vector else ["article_id", "symbol"]

    for day in sel.days_with_articles(con, target.since, target.until):
        pending, n_all = _pending_for_day(con, day, target, directory)
        if remaining is not None:
            pending = pending[:remaining]
        if not pending:
            state = store.write_state(
                directory,
                state,
                [
                    _state_row(
                        day,
                        "up_to_date",
                        n_all,
                        [],
                        0.0,
                        backend,
                        schema,
                        directory=directory,
                    )
                ],
            )
            continue
        totals["days"] += 1
        prior = store.state_lookup(state).get(day, {})
        base_seconds = _num(prior.get("seconds"))
        base_cost = _num(prior.get("cost_usd"))
        day_results: list[Result] = []
        t_day = time.perf_counter()
        stop_reason = ""
        for start in range(0, len(pending), chunk):
            batch = pending[start : start + chunk]
            results = backend.score(batch, schema)
            day_results.extend(results)
            frame = (
                store.vectors_to_frame(results, backend.id, store.now_iso())
                if is_vector
                else store.results_to_frame(
                    results, schema, backend.id, store.now_iso()
                )
            )
            if not frame.empty:
                store.upsert_partition(
                    frame, store.partition_path(directory, day), arrow, keys
                )
            done = start + len(batch)
            elapsed = time.perf_counter() - t_day
            n_ok = sum(1 for r in day_results if r.status == "ok")
            say(
                f"{day}  {done}/{len(pending)}  ok={n_ok} "
                f"invalid={sum(1 for r in day_results if r.status == 'invalid')} "
                f"error={sum(1 for r in day_results if r.status == 'error')}  "
                f"{elapsed / max(done, 1):.1f}s/item"
            )
            state = store.write_state(
                directory,
                state,
                [
                    _state_row(
                        day,
                        "partial",
                        n_all,
                        day_results,
                        elapsed,
                        backend,
                        schema,
                        directory=directory,
                        base_seconds=base_seconds,
                        base_cost=base_cost,
                    )
                ],
            )
            budget_hit = [r for r in results if r.error.startswith("budget:")]
            if budget_hit:
                stop_reason = budget_hit[0].error
                break
        elapsed = time.perf_counter() - t_day
        status = "ok" if not stop_reason else "budget"
        state = store.write_state(
            directory,
            state,
            [
                _state_row(
                    day,
                    status,
                    n_all,
                    day_results,
                    elapsed,
                    backend,
                    schema,
                    stop_reason,
                    directory=directory,
                    base_seconds=base_seconds,
                    base_cost=base_cost,
                )
            ],
        )
        for r in day_results:
            key = f"n_{r.status}"
            if key in totals:
                totals[key] += 1
            totals["cost_usd"] += r.cost_usd
        totals["seconds"] += elapsed
        if remaining is not None:
            remaining -= len(pending)
            if remaining <= 0:
                totals["stopped"] = "limit reached"
                break
        if stop_reason:
            totals["stopped"] = stop_reason
            break
    return totals


def _state_row(
    day: str,
    status: str,
    n_target: int,
    results: list[Result],
    seconds: float,
    backend: Backend,
    schema: Schema,
    detail: str = "",
    *,
    directory: Path,
    base_seconds: float = 0.0,
    base_cost: float = 0.0,
) -> dict[str, Any]:
    # counts are cumulative on disk for the day (a resumed run adds to them)
    on_disk = store.partition_status_counts(store.partition_path(directory, day))
    return {
        "date": day,
        "status": status,
        "n_target": n_target,
        "n_ok": on_disk["ok"],
        "n_invalid": on_disk["invalid"],
        "n_error": on_disk["error"],
        "n_skipped": sum(1 for r in results if r.status == "skipped"),
        "seconds": round(base_seconds + seconds, 1),
        "cost_usd": round(base_cost + sum(r.cost_usd for r in results), 6),
        "model": backend.model,
        "prompt_hash": schema.prompt_hash() if backend.kind == "record" else "",
        "scored_at": store.now_iso(),
        "detail": detail,
    }


def _num(value: Any) -> float:
    try:
        v = float(value)
        return 0.0 if v != v else v
    except (TypeError, ValueError):
        return 0.0
