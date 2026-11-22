"""The auctions store: one prints file per ticker, a daily file, state and audit.

Layout under the Alpaca root::

    auctions/prints/<TICKER>.parquet     every print of one store ticker (PRINT_SCHEMA)
    auctions/auctions_daily.parquet      the chosen crosses, one row per ticker and day
    auctions/auctions_fetch_state.csv    one row per ticker (SymbolState)
    auctions/auctions_fetch_audit.csv    one row per request batch

A fetch replaces a ticker's prints inside the requested date range with what
the vendor returned for that range, so re-reading a range is idempotent and a
restated day replaces the old one. Every write is atomic (``_atomic``).
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from alpaca.auctions import DAILY_SCHEMA, PRINT_COLUMNS, PRINT_SCHEMA

DATASET = "auctions"
STATUS_OK, STATUS_PARTIAL, STATUS_EMPTY, STATUS_ERROR = (
    "ok",
    "partial",
    "empty",
    "error",
)
STATUSES = (STATUS_OK, STATUS_PARTIAL, STATUS_EMPTY, STATUS_ERROR)
_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _iso(value: dt.date | None) -> str:
    return value.isoformat() if value else ""


def _date(value: Any) -> dt.date | None:
    text = (
        ""
        if value is None or (isinstance(value, float) and pd.isna(value))
        else str(value)[:10]
    )
    return dt.date.fromisoformat(text) if text else None


@dataclass(frozen=True)
class SymbolState:
    """What is requested and stored for one fetch unit (a ticker's main symbol, or an alias).

    ``first_data`` / ``last_data`` / ``n_days`` / ``n_prints`` count the prints
    inside the unit's own range. Whether the data covers the ticker's extent
    is a separate question, answered per ticker by :mod:`alpaca.coverage`.
    """

    ticker: str
    symbol: str = ""
    asset_type: str = ""
    asof: dt.date | None = None
    span_from: dt.date | None = None  # what the universe asks for
    span_to: dt.date | None = None
    coverage_from: dt.date | None = None  # what has been requested successfully
    coverage_through: dt.date | None = None
    first_data: dt.date | None = None  # what the stored prints hold
    last_data: dt.date | None = None
    n_days: int = 0
    n_prints: int = 0
    status: str = STATUS_PARTIAL
    fetched_at: str = ""
    detail: str = ""
    unit: str = ""  # the state key; empty means the ticker's main unit
    role: str = "main"  # main | alias

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"status must be one of {STATUSES}, got {self.status!r}")

    @property
    def key(self) -> str:
        return self.unit or self.ticker

    def covers(self, start: dt.date, end: dt.date) -> bool:
        return (
            self.coverage_from is not None
            and self.coverage_through is not None
            and self.coverage_from <= start
            and self.coverage_through >= end
        )

    def to_row(self) -> dict[str, Any]:
        row = asdict(self)
        for key, value in row.items():
            if isinstance(value, dt.date):
                row[key] = value.isoformat()
            elif value is None:
                row[key] = ""
        return row

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "SymbolState":
        def text(key: str) -> str:
            value = row.get(key)
            return (
                ""
                if value is None or (isinstance(value, float) and pd.isna(value))
                else str(value)
            )

        def number(key: str) -> int:
            try:
                return int(float(text(key) or 0))
            except ValueError:
                return 0

        dates = {
            name: _date(row.get(name))
            for name in (
                "asof",
                "span_from",
                "span_to",
                "coverage_from",
                "coverage_through",
                "first_data",
                "last_data",
            )
        }
        return cls(
            ticker=text("ticker"),
            symbol=text("symbol"),
            asset_type=text("asset_type"),
            n_days=number("n_days"),
            n_prints=number("n_prints"),
            status=text("status") or STATUS_ERROR,
            fetched_at=text("fetched_at"),
            detail=text("detail"),
            unit=text("unit"),
            role=text("role") or "main",
            **dates,
        )


STATE_COLUMNS = [f.name for f in fields(SymbolState)]
AUDIT_COLUMNS = [
    "at",
    "start",
    "end",
    "asof",
    "symbols",
    "pages",
    "days",
    "prints",
    "status",
    "detail",
]


@dataclass(frozen=True)
class MergeResult:
    n_days: int
    n_prints: int
    first: dt.date | None
    last: dt.date | None


class AuctionStore:
    """The auctions dataset on disk."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.dir = self.root / DATASET
        self.prints_dir = self.dir / "prints"
        self.daily_path = self.dir / "auctions_daily.parquet"
        self.state_path = self.dir / "auctions_fetch_state.csv"
        self.audit_path = self.dir / "auctions_fetch_audit.csv"
        self.coverage_path = self.dir / "auctions_coverage.csv"

    # ----- prints ---------------------------------------------------------- #
    def prints_path(self, ticker: str) -> Path:
        return self.prints_dir / f"{_UNSAFE.sub('_', ticker)}.parquet"

    def read_prints(self, ticker: str) -> pd.DataFrame:
        path = self.prints_path(ticker)
        if not path.exists():
            return PRINT_SCHEMA.empty_table().to_pandas()
        return pq.read_table(path, schema=PRINT_SCHEMA).to_pandas()

    def tickers_on_disk(self) -> list[str]:
        if not self.prints_dir.is_dir():
            return []
        return sorted(p.stem for p in self.prints_dir.glob("*.parquet"))

    def replace_range(
        self,
        ticker: str,
        frame: pd.DataFrame,
        start: dt.date,
        end: dt.date,
    ) -> MergeResult:
        """Replace the ticker's prints dated ``[start, end]`` by ``frame`` (atomic).

        Prints outside the range belong to the ticker's other units and are
        kept. Returns what the range now holds.
        """
        outside = [d for d in frame["date"] if not start <= d <= end]
        if outside:
            raise ValueError(
                f"{ticker}: prints dated {outside[0]} lie outside {start}..{end}"
            )
        old = self.read_prints(ticker)
        if not old.empty:
            old = old[(old["date"] < start) | (old["date"] > end)]
        parts = [part for part in (old, frame[PRINT_COLUMNS]) if not part.empty]
        self._write_prints(ticker, parts)
        days = frame["date"]
        if days.empty:
            return MergeResult(0, 0, None, None)
        return MergeResult(int(days.nunique()), len(frame), min(days), max(days))

    def stats(self, ticker: str, start: dt.date, end: dt.date) -> MergeResult:
        """What the ticker's stored prints hold inside ``[start, end]``."""
        prints = self.read_prints(ticker)
        days = prints.loc[(prints["date"] >= start) & (prints["date"] <= end), "date"]
        if days.empty:
            return MergeResult(0, 0, None, None)
        return MergeResult(int(days.nunique()), len(days), min(days), max(days))

    def prune(self, ticker: str, ranges: Iterable[tuple[dt.date, dt.date]]) -> int:
        """Drop the ticker's prints outside ``ranges`` (left by an older segmentation)."""
        old = self.read_prints(ticker)
        if old.empty:
            return 0
        spans = list(ranges)
        keep = old["date"].map(lambda d: any(a <= d <= b for a, b in spans))
        if bool(keep.all()):
            return 0
        self._write_prints(ticker, [old[keep]] if bool(keep.any()) else [])
        return int((~keep).sum())

    def _write_prints(self, ticker: str, parts: list[pd.DataFrame]) -> None:
        import _atomic  # type: ignore[import-not-found]

        path = self.prints_path(ticker)
        parts = [part for part in parts if not part.empty]
        if not parts:
            if path.exists():
                path.unlink()
            return
        merged = pd.concat(parts, ignore_index=True) if len(parts) > 1 else parts[0]
        merged = merged.sort_values(["date", "side", "ts"], ignore_index=True)
        table = pa.Table.from_pandas(merged, schema=PRINT_SCHEMA, preserve_index=False)
        _atomic.write_table(table, path, compression="zstd")

    # ----- daily ----------------------------------------------------------- #
    def rebuild_daily(self, select: Callable[[pd.DataFrame], pd.DataFrame]) -> int:
        """Recompute the daily file from every prints file; returns its row count."""
        import _atomic  # type: ignore[import-not-found]

        files = (
            sorted(str(p) for p in self.prints_dir.glob("*.parquet"))
            if self.prints_dir.is_dir()
            else []
        )
        if files:
            import pyarrow.dataset as pads

            # an explicit list: a stray temp file of an interrupted write is never read
            prints = (
                pads.dataset(files, schema=PRINT_SCHEMA, format="parquet")
                .to_table()
                .to_pandas()
            )
        else:
            prints = PRINT_SCHEMA.empty_table().to_pandas()
        daily = select(prints)
        table = pa.Table.from_pandas(daily, schema=DAILY_SCHEMA, preserve_index=False)
        _atomic.write_table(table, self.daily_path, compression="zstd")
        return table.num_rows

    def read_daily(self) -> pd.DataFrame:
        if not self.daily_path.exists():
            return DAILY_SCHEMA.empty_table().to_pandas()
        return pq.read_table(self.daily_path).to_pandas()

    # ----- state and audit -------------------------------------------------- #
    def load_state(self) -> dict[str, SymbolState]:
        if not self.state_path.exists():
            return {}
        frame = pd.read_csv(self.state_path, dtype=str, keep_default_na=False)
        out: dict[str, SymbolState] = {}
        for row in frame.to_dict(orient="records"):
            try:
                state = SymbolState.from_row({str(k): v for k, v in row.items()})
            except ValueError:
                continue
            if state.ticker:
                out[state.key] = state
        return out

    def save_state(self, states: Mapping[str, SymbolState]) -> None:
        import _atomic  # type: ignore[import-not-found]

        rows = [states[t].to_row() for t in sorted(states)]
        _atomic.to_csv(
            pd.DataFrame(rows, columns=STATE_COLUMNS), self.state_path, index=False
        )

    def write_coverage(self, frame: pd.DataFrame) -> None:
        import _atomic  # type: ignore[import-not-found]

        _atomic.to_csv(frame, self.coverage_path, index=False)

    def read_coverage(self) -> pd.DataFrame:
        if not self.coverage_path.exists():
            return pd.DataFrame()
        return pd.read_csv(
            self.coverage_path, dtype={"ticker": str}, keep_default_na=False
        )

    def append_audit(self, rows: Iterable[Mapping[str, Any]]) -> None:
        import _atomic  # type: ignore[import-not-found]

        new = pd.DataFrame(list(rows), columns=AUDIT_COLUMNS)
        if new.empty:
            return
        if self.audit_path.exists():
            old = pd.read_csv(self.audit_path, dtype=str, keep_default_na=False)
            new = pd.concat([old, new.astype(str)], ignore_index=True)
        _atomic.to_csv(new, self.audit_path, index=False)


def with_merge(state: SymbolState, merge: MergeResult) -> SymbolState:
    """The state after a merge: what the stored prints now hold."""
    return replace(
        state,
        first_data=merge.first,
        last_data=merge.last,
        n_days=merge.n_days,
        n_prints=merge.n_prints,
    )
