"""The stored ladders (DD-001 section 7, DD-002 WP9): one parquet file per date.

Two datasets share one build: the short ladder (one file per settlement date)
and the long ladder (one file per 13F period). ``build`` recomputes the whole
ladder from the source views (seconds for the short ladder, minutes for the
long) and replaces only the partitions whose content changed. A row of a date
depends on reports up to that date alone, so a changed digest for an already
stored date means an input was restated (FINRA revised a report, the vendor
changed a price or a split, a 13F amendment arrived inside the window) and is
recorded as such, never overwritten silently. Three facts stay separate: what
the sources hold, what this store holds, and what each build did
(:class:`BuildReport`).
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pyarrow as pa

from finra.store import STATUS_OK, DayState, DayStore
from positioning import long_ladder, short_ladder

SOURCE = "derived"


@dataclass(frozen=True)
class Spec:
    """How one derived dataset is computed, partitioned and checked."""

    name: str
    subdir: str
    schema: pa.Schema
    partition: str  # the date column one file holds
    key: tuple[str, ...]  # unique per row
    symbol: str  # the column counted as "symbols"
    compute: Callable[[Any], pd.DataFrame]
    source_latest: Callable[[Any], dt.date | None]
    build_hint: str
    source_hint: str

    @property
    def columns(self) -> list[str]:
        return list(self.schema.names)


def _si_latest(con: Any) -> dt.date | None:
    row = con.execute(f"SELECT max(settlement_date) FROM {short_ladder.SI_VIEW}").fetchone()
    return None if row is None or row[0] is None else pd.Timestamp(row[0]).date()


SHORT_LADDER = Spec(
    name="short_ladder",
    subdir="settlement",
    schema=pa.schema(
        [
            ("eodhd_code", pa.string()),
            ("settlement_date", pa.date32()),
            ("published_at", pa.date32()),
            ("lane", pa.string()),
            ("inventory", pa.float64()),
            ("flow", pa.float64()),
            ("n_lots", pa.int32()),
            ("wavg_age_days", pa.float64()),
            ("cost_basis", pa.float64()),
            ("profit_pct", pa.float64()),
            ("unrealised", pa.float64()),
            ("realised", pa.float64()),
            ("seed_share", pa.float64()),
            ("gap", pa.bool_()),
            ("reset", pa.bool_()),
            ("quantity_factor", pa.float64()),
            ("price_factor", pa.float64()),
        ]
    ),
    partition="settlement_date",
    key=("eodhd_code", "settlement_date"),
    symbol="eodhd_code",
    compute=short_ladder.compute,
    source_latest=_si_latest,
    build_hint="positioning build --run",
    source_hint="short interest",
)
LONG_LADDER = Spec(
    name=long_ladder.NAME,
    subdir="period",
    schema=long_ladder.SCHEMA,
    partition="period",
    key=long_ladder.KEY,
    symbol="cusip",
    compute=long_ladder.compute,
    source_latest=long_ladder.source_latest,
    build_hint="positioning build --dataset long_ladder --run",
    source_hint="13F holdings",
)
SPECS: dict[str, Spec] = {s.name: s for s in (SHORT_LADDER, LONG_LADDER)}
DATASETS: tuple[str, ...] = tuple(SPECS)

# the short ladder's names, kept for its callers
NAME = SHORT_LADDER.name
SUBDIR = SHORT_LADDER.subdir
SCHEMA = SHORT_LADDER.schema
COLUMNS = SHORT_LADDER.columns


def spec_of(dataset: str | Spec) -> Spec:
    if isinstance(dataset, Spec):
        return dataset
    try:
        return SPECS[dataset]
    except KeyError:
        raise ValueError(f"unknown positioning dataset {dataset!r}; one of {', '.join(DATASETS)}") from None


def store(root: Path, dataset: str | Spec = SHORT_LADDER) -> DayStore:
    spec = spec_of(dataset)
    return DayStore(root, spec.name, spec.schema, subdir=spec.subdir)


@dataclass(frozen=True)
class BuildReport:
    dataset: str
    run: bool
    root: Path
    partitions: int
    rows: int
    symbols: int
    new: tuple[dt.date, ...]
    restated: tuple[dt.date, ...]
    unchanged: int
    orphans: tuple[dt.date, ...]
    stats: dict[str, int] | None = None

    @property
    def ok(self) -> bool:
        return True

    @property
    def changed(self) -> int:
        return len(self.new) + len(self.restated)


def digest(frame: pd.DataFrame, spec: Spec = SHORT_LADDER) -> str:
    """Content digest of one partition, stable across runs and row order."""
    ordered = frame[spec.columns].sort_values(list(spec.key), kind="stable")
    text = ordered.to_csv(index=False, float_format="%.12g", lineterminator="\n")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build(
    con: Any,
    root: Path,
    *,
    run: bool,
    now: dt.datetime | None = None,
    dataset: str | Spec = SHORT_LADDER,
) -> BuildReport:
    """Recompute the ladder and, with ``run``, write the partitions that changed."""
    spec = spec_of(dataset)
    frame = spec.compute(con)
    stats = frame.attrs.get("stats") if hasattr(frame, "attrs") else None
    target = store(root, spec)
    state = target.load_state()
    on_disk = set(target.days_on_disk())
    if frame.empty:
        return BuildReport(spec.name, run, root, 0, 0, 0, (), (), 0, tuple(sorted(on_disk)), stats)
    frame = frame[spec.columns]
    stamp = (now or dt.datetime.now(dt.timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new: list[dt.date] = []
    restated: list[dt.date] = []
    unchanged = 0
    seen: set[dt.date] = set()
    states: list[DayState] = []
    for day, part in frame.groupby(spec.partition, sort=True):
        day = pd.Timestamp(day).date()
        seen.add(day)
        sha = digest(part, spec)
        previous = state.get(day.isoformat())
        if previous is not None and previous.sha256 == sha and day in on_disk:
            unchanged += 1
            continue
        is_restatement = previous is not None and bool(previous.sha256)
        (restated if is_restatement else new).append(day)
        if not run:
            continue
        target.write_day(day, part)
        detail = ""
        if is_restatement:
            assert previous is not None
            detail = (
                f"restated {stamp[:10]}: was {previous.rows} rows, "
                f"sha {previous.sha256[:12]}"
            )
        states.append(
            DayState(
                date=day.isoformat(),
                status=STATUS_OK,
                source=SOURCE,
                rows=int(len(part)),
                sha256=sha,
                fetched_at=stamp,
                detail=detail,
            )
        )
    if states:
        target.upsert_state(states)
    return BuildReport(
        dataset=spec.name,
        run=run,
        root=root,
        partitions=len(seen),
        rows=int(len(frame)),
        symbols=int(frame[spec.symbol].nunique()),
        new=tuple(new),
        restated=tuple(restated),
        unchanged=unchanged,
        orphans=tuple(sorted(on_disk - seen)),
        stats=stats,
    )


@dataclass(frozen=True)
class Finding:
    severity: str  # error | warn
    check: str
    detail: str


def qc(root: Path, con: Any | None = None, dataset: str | Spec = SHORT_LADDER) -> list[Finding]:
    """Checks over a stored ladder; with ``con`` also against the source views."""
    spec = spec_of(dataset)
    target = store(root, spec)
    findings: list[Finding] = []
    days = target.days_on_disk()
    if not days:
        return [Finding("warn", "empty", f"no {spec.name} stored; run `{spec.build_hint}`")]
    state = target.load_state()
    missing_state = [d for d in days if d.isoformat() not in state]
    if missing_state:
        findings.append(
            Finding("error", "state_missing", f"{len(missing_state)} partitions without a state row")
        )
    missing_file = [k for k, s in state.items() if s.status == STATUS_OK and s.day not in set(days)]
    if missing_file:
        findings.append(
            Finding("error", "file_missing", f"{len(missing_file)} state rows without a file")
        )
    frame = target.read_range()
    dupes = int(frame.duplicated(list(spec.key)).sum())
    if dupes:
        findings.append(Finding("error", "duplicate_key", f"{dupes} duplicate {spec.key} rows"))
    held = frame[frame["inventory"] > 0]
    null_state = int(held[["wavg_age_days", "cost_basis", "profit_pct"]].isna().any(axis=1).sum())
    if null_state:
        findings.append(Finding("error", "null_with_inventory", f"{null_state} held rows lack age, cost or profit"))
    empty = frame[frame["inventory"] <= 0]
    filled = int(empty[["wavg_age_days", "cost_basis", "profit_pct"]].notna().any(axis=1).sum())
    if filled:
        findings.append(Finding("error", "filled_without_inventory", f"{filled} empty rows carry age, cost or profit"))
    early = int((pd.to_datetime(frame["published_at"]) <= pd.to_datetime(frame[spec.partition])).sum())
    if early:
        findings.append(Finding("error", f"published_before_{spec.partition}", f"{early} rows"))
    negative_age = int((frame["wavg_age_days"] < 0).sum())
    if negative_age:
        findings.append(Finding("error", "negative_age", f"{negative_age} rows"))
    mismatch = frame[frame["quantity_factor"] != frame["price_factor"]]
    if len(mismatch):
        findings.append(
            Finding(
                "warn",
                "price_only_adjustments",
                f"{mismatch[spec.symbol].nunique()} symbols carry a vendor split entry "
                "not applied to share counts (spin-off or merger adjustment)",
            )
        )
    series = frame[list(spec.key[:-1])].drop_duplicates() if spec is LONG_LADDER else frame[[spec.symbol]].drop_duplicates()
    resets = int(frame["reset"].sum()) - int(len(series))
    if resets > 0:
        findings.append(Finding("warn", "resets", f"{resets} ladders restarted after a long absence"))
    restated = [s for s in state.values() if s.detail.startswith("restated")]
    if restated:
        findings.append(Finding("warn", "restated", f"{len(restated)} partitions were restated; latest {max(s.date for s in restated)}"))
    if con is not None:
        try:
            latest = spec.source_latest(con)
        except Exception:
            latest = None
        if latest is not None and latest > days[-1]:
            findings.append(
                Finding(
                    "warn",
                    "stale",
                    f"{spec.source_hint} reaches {latest}, the ladder {days[-1]}; "
                    f"run `{spec.build_hint}`",
                )
            )
        if spec is LONG_LADDER:
            findings.extend(_qc_long(frame, con))
    return findings


def _qc_long(frame: pd.DataFrame, con: Any) -> list[Finding]:
    """The long ladder's reconciliation: every stored level equals the source's aggregate."""
    findings: list[Finding] = []
    try:
        compared, differing = long_ladder.reconcile(frame, con)
    except Exception as exc:  # the sec views may be absent on this connection
        return [Finding("warn", "reconcile_skipped", f"could not recompute the aggregates: {exc}")]
    if differing:
        findings.append(
            Finding(
                "error",
                "level_mismatch",
                f"{differing} of {compared} rows differ from the sum of effective holdings",
            )
        )
    lag = (pd.to_datetime(frame["published_at"]) - pd.to_datetime(frame["period"])).dt.days
    if len(lag) and (lag.max() > long_ladder.LATE_DAYS or lag.min() < long_ladder.DEADLINE_DAYS):
        findings.append(
            Finding("error", "published_at_window", f"published_at lags range {lag.min()}..{lag.max()} days")
        )
    inventory_mismatch = frame[(frame["inventory"] - frame["level_raw"] / frame["quantity_factor"]).abs() > 1e-6]
    if len(inventory_mismatch):
        findings.append(
            Finding("error", "inventory_not_level", f"{len(inventory_mismatch)} rows where inventory differs from the basis level")
        )
    return findings


def status(root: Path, dataset: str | Spec = SHORT_LADDER) -> dict[str, Any]:
    spec = spec_of(dataset)
    target = store(root, spec)
    days = target.days_on_disk()
    state = target.load_state()
    last_build = max((s.fetched_at for s in state.values()), default="")
    return {
        "dataset": spec.name,
        "present": bool(days),
        "partitions": len(days),
        "first": days[0].isoformat() if days else None,
        "last": days[-1].isoformat() if days else None,
        "rows": sum(s.rows for s in state.values()),
        "restated": sum(1 for s in state.values() if s.detail.startswith("restated")),
        "last_build": last_build or None,
    }
