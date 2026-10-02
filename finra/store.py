"""A partition-per-file parquet store with a fetch-state sidecar.

Layout for dataset ``<name>`` under the FINRA root::

    <root>/<name>/<subdir>/YYYY-MM-DD.parquet        one file per date partition
    <root>/<name>/<subdir>/YYYY-MM-DD_<part>.parquet  ... or per (date, part)
    <root>/<name>/<name>_fetch_state.csv              one row per attempted partition

A partition is a date (a trade date for daily short volume) with an optional
``part`` (a tier for the weekly flow data, whose API partition key is the
pair ``(weekStartDate, tierIdentifier)``). One file per partition (the
``news/articles`` precedent) keeps presence, idempotency and restatement
trivial: re-fetching a partition replaces one file atomically. Every file is
written under a pinned pyarrow schema so DuckDB can union the whole history
without schema drift, and the date column statistics let it skip files a date
predicate excludes.

The state sidecar is the fast index the planner and ``status`` read; it never
holds data, only facts about each attempt (:class:`DayState`). ``part`` is
empty for a plain date partition, so the short volume sidecar is unchanged.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Iterable, Mapping

import pandas as pd
import pyarrow as pa

STATUS_OK = "ok"
STATUS_ABSENT = "absent"
STATUS_ERROR = "error"
STATUSES = (STATUS_OK, STATUS_ABSENT, STATUS_ERROR)


def partition_key(day: dt.date | str, part: str = "") -> str:
    """``2026-09-07`` or ``2026-09-07/T1``: how a partition is named in state."""
    iso = day if isinstance(day, str) else day.isoformat()
    return f"{iso}/{part}" if part else iso


@dataclass(frozen=True)
class DayState:
    """What the last attempt at one partition established."""

    date: str  # ISO YYYY-MM-DD
    status: str  # ok | absent | error
    source: str = ""  # cdn | api | ""
    rows: int = 0
    short_sum: int = 0  # dataset-specific summary sums (rounded)
    total_sum: int = 0
    sha256: str = ""  # digest of the stored content (restatement detection)
    fetched_at: str = ""  # ISO UTC
    detail: str = ""
    part: str = ""  # secondary partition value (e.g. a tier), "" for none

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")
        dt.date.fromisoformat(self.date)  # ValueError on a bad date

    @property
    def day(self) -> dt.date:
        return dt.date.fromisoformat(self.date)

    @property
    def key(self) -> str:
        return partition_key(self.date, self.part)

    def to_row(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_row(cls, row: Mapping[Any, Any]) -> "DayState":
        def _int(value: Any) -> int:
            try:
                return int(float(value))
            except (TypeError, ValueError):
                return 0

        def _str(value: Any) -> str:
            if value is None or (isinstance(value, float) and pd.isna(value)):
                return ""
            return str(value)

        return cls(
            date=_str(row.get("date"))[:10],
            status=_str(row.get("status")) or STATUS_ERROR,
            source=_str(row.get("source")),
            rows=_int(row.get("rows")),
            short_sum=_int(row.get("short_sum")),
            total_sum=_int(row.get("total_sum")),
            sha256=_str(row.get("sha256")),
            fetched_at=_str(row.get("fetched_at")),
            detail=_str(row.get("detail")),
            part=_str(row.get("part")),
        )


STATE_COLUMNS = [f.name for f in fields(DayState)]


class DayStore:
    """One dataset's partition files plus its state sidecar. All writes are atomic."""

    def __init__(
        self, root: Path, name: str, schema: pa.Schema, *, subdir: str = "daily"
    ) -> None:
        self.root = Path(root)
        self.name = name
        self.schema = schema
        self.dir = self.root / name
        self.daily_dir = self.dir / subdir
        self.state_path = self.dir / f"{name}_fetch_state.csv"

    # ----- partition files ------------------------------------------------- #
    def day_path(self, day: dt.date, part: str = "") -> Path:
        stem = f"{day.isoformat()}_{part}" if part else day.isoformat()
        return self.daily_dir / f"{stem}.parquet"

    def write_day(self, day: dt.date, frame: pd.DataFrame, part: str = "") -> Path:
        """Write ``frame`` as the partition's file under the pinned schema (atomic replace)."""
        import _atomic  # type: ignore[import-not-found]

        expected = list(self.schema.names)
        missing = [c for c in expected if c not in frame.columns]
        if missing:
            raise ValueError(
                f"{self.name} {partition_key(day, part)}: frame lacks columns {missing}"
            )
        table = pa.Table.from_pandas(
            frame[expected], schema=self.schema, preserve_index=False
        )
        path = self.day_path(day, part)
        _atomic.write_table(table, path, compression="zstd")
        return path

    def read_day(self, day: dt.date, part: str = "") -> pd.DataFrame | None:
        path = self.day_path(day, part)
        return pd.read_parquet(path) if path.exists() else None

    def partitions_on_disk(self) -> list[tuple[dt.date, str]]:
        """Every ``(date, part)`` with a file, ascending by date then part."""
        if not self.daily_dir.is_dir():
            return []
        found: list[tuple[dt.date, str]] = []
        for path in self.daily_dir.glob("*.parquet"):
            stem, _, part = path.stem.partition("_")
            try:
                found.append((dt.date.fromisoformat(stem), part))
            except ValueError:
                continue  # not one of ours
        return sorted(found)

    def days_on_disk(self) -> list[dt.date]:
        """Dates with a plain (no ``part``) file, ascending."""
        return [day for day, part in self.partitions_on_disk() if not part]

    def read_range(
        self, start: dt.date | None = None, end: dt.date | None = None
    ) -> pd.DataFrame:
        """Concatenate every partition inside ``[start, end]`` (empty frame if none)."""
        frames = [
            frame
            for day, part in self.partitions_on_disk()
            if (start is None or day >= start) and (end is None or day <= end)
            if (frame := self.read_day(day, part)) is not None
        ]
        if not frames:
            return pd.DataFrame(columns=list(self.schema.names))
        return pd.concat(frames, ignore_index=True)

    # ----- state ----------------------------------------------------------- #
    def load_state(self) -> dict[str, DayState]:
        """``{partition_key: DayState}`` from the sidecar (empty when absent or unreadable)."""
        if not self.state_path.exists():
            return {}
        try:
            frame = pd.read_csv(self.state_path, dtype=str, keep_default_na=False)
        except Exception:
            return {}
        out: dict[str, DayState] = {}
        for row in frame.to_dict(orient="records"):
            try:
                state = DayState.from_row(row)
            except ValueError:
                continue
            out[state.key] = state
        return out

    def upsert_state(self, states: Iterable[DayState]) -> None:
        """Merge ``states`` over the sidecar by partition key and rewrite it atomically."""
        import _atomic  # type: ignore[import-not-found]

        merged = self.load_state()
        for state in states:
            merged[state.key] = state
        rows = [merged[key].to_row() for key in sorted(merged)]
        frame = pd.DataFrame(rows, columns=STATE_COLUMNS)
        _atomic.to_csv(frame, self.state_path, index=False)

    def state_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state in self.load_state().values():
            counts[state.status] = counts.get(state.status, 0) + 1
        return counts
