"""Fetch 1-minute bars for the ``intraday`` lane from EODHD.

Each request covers at most ``WINDOW_DAYS`` days of one pair and costs
``UNITS_PER_REQUEST`` units. Per pair the held span is the single interval
``coverage_from .. coverage_through`` (UTC dates); every request extends it at
one end. A run goes in this order, so a capped run always keeps every pair
current before it fills any history:

1. top-up of pairs that already have coverage (re-reading the last
   ``OVERLAP_DAYS`` days for late corrections);
2. the newest window of pairs with no state yet;
3. history windows across all pairs, newest window first.

History is never requested before a pair's first daily price (the universe
build clips ``fetch_from``); ``EMPTY_STREAK_STOP`` consecutive empty history
windows older than a pair's first bar end its backfill (the vendor's 1m
history starts there, e.g. LSE about 2021). HTTP 404 marks a pair ``not_found``; other failures mark it ``error``
and skip it for the rest of the run. HTTP 401/403 aborts the run.

Targets come from ``universe_membership.parquet`` (``build_intraday_universe.py``),
plus two hand-kept corrections from ``intraday_universe.toml``:

- ``[[alias]]``: EODHD files a renamed company's 1m history under the symbol in
  use at the time (BHGE for Baker Hughes until 2019-10-17), while the daily
  lanes and so the universe use the current one (BKR). An alias adds a state
  row ``ticker=BKR, source=BHGE`` whose windows request ``BHGE.US`` and store
  the bars as ``BKR.US``, replacing whatever ``BKR.US`` held in that window.
- ``[[mask]]``: spans where a symbol's 1m bars are another security (FI.US
  before 2023-06 is Frank's International, not Fiserv). Masked bars are
  deleted and never requested again.

Bars on early-close days (``early_close``) are tagged ``post`` after the
close; ``--retag-sessions`` rewrites stored bars to that rule (local, no API).

Every run that is not a dry run ends by rebuilding the US volume fix
(``intraday_volume``: the vendor's closing-cross and report artefacts against
the daily volume); ``--volume-fix`` rebuilds it alone (local, no API).

Design: docs/INTRADAY_1M_DESIGN.md, section 6.

Usage:
    uv run python eodhd/fetch_eodhd_intraday.py --dry-run          # plan + units, spends nothing
    uv run python eodhd/fetch_eodhd_intraday.py --max-units 25000  # what `refresh intraday --run` does
    uv run python eodhd/fetch_eodhd_intraday.py --tickers NVDA.US MU.US
    uv run python eodhd/fetch_eodhd_intraday.py --retag-sessions   # local: half-day session tags
    uv run python eodhd/fetch_eodhd_intraday.py --volume-fix       # local: rebuild the volume fix

Outputs (under <data_root>/intraday/):
    bars_1m/<TICKER>.<EXCH>_<YYYY>.parquet
    bars_1m_fetch_state.csv
    bars_1m_fetch_audit.csv
    bars_1m_volume_days.parquet, bars_1m_volume_fix.parquet   (intraday_volume)
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pandas as pd
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _atomic  # noqa: E402
import build_intraday_universe as universe  # noqa: E402
from _datadir import EODHD_RAW_ROOT  # noqa: E402

log = logging.getLogger("eodhd_intraday")

BASE_URL = "https://eodhd.com/api/intraday"
INTERVAL = "1m"
UNITS_PER_REQUEST = 5
WINDOW_DAYS = 118  # the API caps a 1m request at 120 days
OVERLAP_DAYS = 3
#: Consecutive empty history windows (each ~4 months) that end a backfill. Bars
#: are never requested before a pair's first daily price (the universe build
#: clips fetch_from), so this is only the backstop for vendor-side history
#: starts (LSE 1m begins about 2021) -- not a listing detector: EODHD has
#: multi-window holes (TSLA 2023-08 .. 2024-04).
EMPTY_STREAK_STOP = 8
REQUEST_TIMEOUT = 120
#: Pairs fetched at once. A full 1m window is ~10 MB of JSON and takes ~8 s,
#: almost all of it server and transfer time, so a few in flight multiply the
#: throughput while staying far below EODHD's 1,000 requests a minute.
DEFAULT_WORKERS = 6

RAW_DIR = EODHD_RAW_ROOT / universe.LANE
BARS_DIR = RAW_DIR / "bars_1m"
STATE_PATH = RAW_DIR / "bars_1m_fetch_state.csv"
AUDIT_PATH = RAW_DIR / "bars_1m_fetch_audit.csv"

BAR_COLUMNS = [
    "ticker",
    "exchange",
    "ts",
    "date",
    "session",
    "open",
    "high",
    "low",
    "close",
    "volume",
]
STATE_COLUMNS = [
    "ticker",
    "exchange",
    "fetch_from",
    "fetch_to",
    "coverage_from",
    "coverage_through",
    "first_data_date",
    "latest_data_date",
    "n_rows",
    "fetched_at",
    "status",
    "last_error",
    "source",
]
AUDIT_COLUMNS = [
    "requested_at",
    "ticker",
    "exchange",
    "phase",
    "from_date",
    "to_date",
    "http_status",
    "rows",
    "units",
    "error",
    "source",
]

#: exchange -> (timezone, regular-session open, regular-session close); ``None``
#: open means a 24h market (every bar is ``24h``). Unknown exchanges are 24h UTC.
SESSION_RULES: dict[str, tuple[str, time | None, time | None]] = {
    "US": ("America/New_York", time(9, 30), time(16, 0)),
    "INDX": ("America/New_York", time(9, 30), time(16, 0)),
    "LSE": ("Europe/London", time(8, 0), time(16, 30)),
    "FOREX": ("UTC", None, None),
}


#: exchange -> early close (local time) on the days ``early_close`` names.
EARLY_CLOSE: dict[str, time] = {
    "US": time(13, 0),
    "INDX": time(13, 0),
    "LSE": time(12, 30),
}


def _thanksgiving(year: int) -> date:
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def early_close(exchange: str, day: date) -> time | None:
    """The early regular-session close on ``day``, or ``None`` on a normal day.

    NYSE / Nasdaq (and the Cboe / S&P indices) close at 13:00 ET on the day
    after Thanksgiving, and on 24 December and 3 July when those fall Monday to
    Thursday (on a Friday they are the observed holiday). The LSE closes at
    12:30 on weekday 24 and 31 December. Rule-based; exceptional closes (a
    national day of mourning) are full closures, not half days.
    """
    close = EARLY_CLOSE.get(exchange)
    if close is None or day.weekday() >= 5:
        return None
    if exchange == "LSE":
        return close if (day.month, day.day) in {(12, 24), (12, 31)} else None
    if day == _thanksgiving(day.year) + timedelta(days=1):
        return close
    if (day.month, day.day) in {(12, 24), (7, 3)} and day.weekday() <= 3:
        return close
    return None


def tag_sessions(local: pd.Series, exchange: str) -> pd.Series:
    """``pre`` / ``rth`` / ``post`` (``24h`` for 24h markets) for local bar times."""
    _tz, rth_open, rth_close = SESSION_RULES.get(exchange, ("UTC", None, None))
    if rth_open is None:
        return pd.Series("24h", index=local.index)
    clock = local.dt.time
    close = pd.Series(rth_close, index=local.index, dtype=object)
    if exchange in EARLY_CLOSE:
        early = local.dt.date.map(lambda d: early_close(exchange, d))
        close = close.where(early.isna(), early)
    rth = pd.Series(
        [rth_open <= c < e for c, e in zip(clock, close)], index=local.index, dtype=bool
    )
    session = pd.Series("post", index=local.index)
    session[clock < rth_open] = "pre"
    session[rth] = "rth"
    return session


# --------------------------------------------------------------------------- #
# bars
# --------------------------------------------------------------------------- #
def bars_from_payload(
    payload: list[dict[str, Any]], ticker: str, exchange: str
) -> pd.DataFrame:
    """Vendor rows -> bar rows (``BAR_COLUMNS``): UTC ``ts``, exchange-local
    trading ``date`` and ``session``; prices and volume as delivered."""
    if not payload:
        return pd.DataFrame(columns=BAR_COLUMNS)
    raw = pd.DataFrame(payload)
    ts = pd.to_datetime(raw["timestamp"].astype("int64"), unit="s", utc=True).astype(
        "datetime64[ms, UTC]"
    )
    tz = SESSION_RULES.get(exchange, ("UTC", None, None))[0]
    local = ts.dt.tz_convert(tz)
    session = tag_sessions(local, exchange)
    out = pd.DataFrame(
        {
            "ticker": ticker,
            "exchange": exchange,
            "ts": ts,
            "date": local.dt.strftime("%Y-%m-%d"),
            "session": session,
        }
    )
    for col in ("open", "high", "low", "close", "volume"):
        out[col] = pd.to_numeric(raw.get(col), errors="coerce").astype("float64")
    return (
        out[BAR_COLUMNS]
        .sort_values("ts")
        .drop_duplicates("ts", keep="last")
        .reset_index(drop=True)
    )


def partition_path(bars_dir: Path, ticker: str, exchange: str, year: int | str) -> Path:
    return bars_dir / f"{ticker}.{exchange}_{year}.parquet"


def pair_files(bars_dir: Path, ticker: str, exchange: str) -> list[Path]:
    prefix = f"{ticker}.{exchange}_"
    if not bars_dir.exists():
        return []
    return sorted(
        p
        for p in bars_dir.glob(f"{prefix}*.parquet")
        if p.name[len(prefix) : -len(".parquet")].isdigit()
    )


def _utc_bounds(start: date, end: date) -> tuple[pd.Timestamp, pd.Timestamp]:
    """``[start 00:00, end + 1 day 00:00)`` UTC: the span a window requests."""
    return (
        pd.Timestamp(start, tz="UTC"),
        pd.Timestamp(end + timedelta(days=1), tz="UTC"),
    )


def _file_year(path: Path) -> int:
    return int(path.stem.rsplit("_", 1)[1])


def drop_span(
    bars_dir: Path, ticker: str, exchange: str, start: date, end: date
) -> int:
    """Delete a pair's stored bars inside ``[start, end]`` (UTC dates); returns the count."""
    lo, hi = _utc_bounds(start, end)
    dropped = 0
    for path in pair_files(bars_dir, ticker, exchange):
        if not start.year - 1 <= _file_year(path) <= end.year + 1:
            continue
        old = pd.read_parquet(path)
        keep = old[(old["ts"] < lo) | (old["ts"] >= hi)]
        if len(keep) == len(old):
            continue
        dropped += len(old) - len(keep)
        if keep.empty:
            path.unlink()
        else:
            _atomic.to_parquet(keep, path, index=False, compression="zstd")
    return dropped


def merge_bars(
    bars_dir: Path, bars: pd.DataFrame, *, replace: tuple[date, date] | None = None
) -> int:
    """Merge bars into their ticker-year files (last copy of a ``ts`` wins).

    With ``replace`` (an alias window's UTC dates), the pair's stored bars
    inside that span are dropped first, so the window's bars replace them.
    Returns the number of genuinely new bar timestamps.
    """
    if bars.empty:
        return 0
    added = 0
    ticker, exchange = str(bars["ticker"].iloc[0]), str(bars["exchange"].iloc[0])
    if replace is not None:
        drop_span(bars_dir, ticker, exchange, *replace)
    for year, part in bars.groupby(bars["date"].str[:4]):
        path = partition_path(bars_dir, ticker, exchange, year)
        if path.exists():
            old = pd.read_parquet(path)
            before = set(old["ts"])
            merged = pd.concat([old, part], ignore_index=True)
        else:
            before = set()
            merged = part
        merged = (
            merged.drop_duplicates("ts", keep="last")
            .sort_values("ts")
            .reset_index(drop=True)
        )
        added += int((~part["ts"].isin(before)).sum())
        _atomic.to_parquet(merged[BAR_COLUMNS], path, index=False, compression="zstd")
    return added


def count_rows(bars_dir: Path, ticker: str, exchange: str) -> int:
    return sum(
        int(pq.ParquetFile(p).metadata.num_rows)
        for p in pair_files(bars_dir, ticker, exchange)
    )


def count_rows_between(
    bars_dir: Path, ticker: str, exchange: str, start: date, end: date
) -> int:
    """Stored bars of a pair inside ``[start, end]`` (UTC dates)."""
    lo, hi = _utc_bounds(start, end)
    total = 0
    for path in pair_files(bars_dir, ticker, exchange):
        if not start.year - 1 <= _file_year(path) <= end.year + 1:
            continue
        ts = pd.read_parquet(path, columns=["ts"])["ts"]
        total += int(((ts >= lo) & (ts < hi)).sum())
    return total


def retag_sessions(bars_dir: Path) -> tuple[int, int]:
    """Re-derive ``session`` for stored bars on early-close days (local, idempotent).

    Only files whose year holds an early-close day are read. Returns
    ``(files rewritten, bars retagged)``.
    """
    files = changed = 0
    for path in sorted(bars_dir.glob("*.parquet")):
        stem = path.stem
        pair, _, year = stem.rpartition("_")
        exchange = pair.rpartition(".")[2]
        if exchange not in EARLY_CLOSE or not year.isdigit():
            continue
        frame = pd.read_parquet(path)
        if frame.empty:
            continue
        tz = SESSION_RULES[exchange][0]
        local = frame["ts"].dt.tz_convert(tz)
        fresh = tag_sessions(local, exchange)
        diff = int((fresh != frame["session"]).sum())
        if diff:
            frame["session"] = fresh.values
            _atomic.to_parquet(
                frame[BAR_COLUMNS], path, index=False, compression="zstd"
            )
            files += 1
            changed += diff
    return files, changed


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=STATE_COLUMNS)
    state = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in STATE_COLUMNS:
        if col not in state.columns:
            state[col] = ""
    return state[STATE_COLUMNS]


def _d(value: Any) -> date | None:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() in {"nan", "nat", "none"}:
        return None
    return date.fromisoformat(text[:10])


def _iso(value: date | None) -> str:
    return value.isoformat() if value else ""


@dataclass
class PairState:
    """One state row: a pair, or an alias of a pair (``source`` = the symbol
    requested; bars are stored under ``ticker``)."""

    ticker: str
    exchange: str
    fetch_from: date
    fetch_to: date | None = None
    coverage_from: date | None = None
    coverage_through: date | None = None
    first_data_date: date | None = None
    latest_data_date: date | None = None
    n_rows: int = 0
    fetched_at: str = ""
    status: str = ""
    last_error: str = ""
    source: str = ""

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ticker, self.exchange, self.source)

    @property
    def symbol(self) -> str:
        """The symbol requested from the API."""
        return self.source or self.ticker

    def target_end(self, to_date: date) -> date:
        return min(to_date, self.fetch_to) if self.fetch_to else to_date

    def history_done(self) -> bool:
        return self.coverage_from is not None and self.coverage_from <= self.fetch_from

    def settle_status(self, to_date: date) -> None:
        if self.status in {"not_found", "error"}:
            return
        if self.coverage_from is None:
            self.status = ""
        elif not self.history_done():
            self.status = "partial"
        elif self.first_data_date is None:
            self.status = "empty"
        else:
            self.status = "ok"

    def row(self) -> dict[str, str]:
        return {
            "ticker": self.ticker,
            "exchange": self.exchange,
            "fetch_from": _iso(self.fetch_from),
            "fetch_to": _iso(self.fetch_to),
            "coverage_from": _iso(self.coverage_from),
            "coverage_through": _iso(self.coverage_through),
            "first_data_date": _iso(self.first_data_date),
            "latest_data_date": _iso(self.latest_data_date),
            "n_rows": str(self.n_rows),
            "fetched_at": self.fetched_at,
            "status": self.status,
            "last_error": self.last_error,
            "source": self.source,
        }


def _source(value: Any) -> str:
    text = "" if value is None else str(value).strip()
    return "" if text.lower() in {"", "nan", "none"} else text


def state_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row["ticker"]), str(row["exchange"]), _source(row.get("source")))


def merge_targets(
    targets: pd.DataFrame, state: pd.DataFrame
) -> dict[tuple[str, str, str], PairState]:
    """One :class:`PairState` per target (pair or alias), carrying over the saved state.

    State rows no longer targeted are kept (history is never dropped) but are
    not fetched.
    """
    saved = {state_key(r): r for r in state.to_dict("records")}
    pairs: dict[tuple[str, str, str], PairState] = {}
    for r in targets.to_dict("records"):
        key = state_key(r)
        old = saved.get(key, {})
        pairs[key] = PairState(
            ticker=key[0],
            exchange=key[1],
            source=key[2],
            fetch_from=_d(r["fetch_from"]) or date(2016, 1, 1),
            fetch_to=_d(r.get("fetch_to")),
            coverage_from=_d(old.get("coverage_from")),
            coverage_through=_d(old.get("coverage_through")),
            first_data_date=_d(old.get("first_data_date")),
            latest_data_date=_d(old.get("latest_data_date")),
            n_rows=int(old.get("n_rows") or 0),
            fetched_at=str(old.get("fetched_at") or ""),
            status=str(old.get("status") or ""),
            last_error=str(old.get("last_error") or ""),
        )
    return pairs


def state_frame(
    pairs: dict[tuple[str, str, str], PairState], previous: pd.DataFrame
) -> pd.DataFrame:
    rows = {p.key: p.row() for p in pairs.values()}
    for r in previous.to_dict("records"):
        rows.setdefault(
            state_key(r),
            {
                c: _source(r.get(c, "")) if c == "source" else str(r.get(c, ""))
                for c in STATE_COLUMNS
            },
        )
    frame = pd.DataFrame(list(rows.values()), columns=STATE_COLUMNS)
    return frame.sort_values(["exchange", "ticker", "source"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    ticker: str  # stored under
    exchange: str
    start: date  # inclusive, UTC date
    end: date  # inclusive, UTC date
    phase: str  # topup | first | backfill
    source: str = ""  # requested symbol when an alias

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.ticker, self.exchange, self.source)

    @property
    def pair(self) -> tuple[str, str]:
        """The stored pair: its windows (own and alias) never run at once."""
        return (self.ticker, self.exchange)


def chunk_down(
    newest: date, oldest: date, days: int = WINDOW_DAYS
) -> list[tuple[date, date]]:
    """``[newest .. oldest]`` as windows of at most ``days`` days, newest first."""
    out: list[tuple[date, date]] = []
    end = newest
    while end >= oldest:
        start = max(oldest, end - timedelta(days=days - 1))
        out.append((start, end))
        end = start - timedelta(days=1)
    return out


def chunk_up(
    oldest: date, newest: date, days: int = WINDOW_DAYS
) -> list[tuple[date, date]]:
    """``[oldest .. newest]`` as windows of at most ``days`` days, oldest first
    (so each window extends ``coverage_through`` contiguously)."""
    out: list[tuple[date, date]] = []
    start = oldest
    while start <= newest:
        end = min(newest, start + timedelta(days=days - 1))
        out.append((start, end))
        start = end + timedelta(days=1)
    return out


def plan_windows(
    pairs: Iterable[PairState],
    *,
    to_date: date,
    full_refresh: bool = False,
    retry_not_found: bool = False,
) -> list[Window]:
    """All windows the pairs still need, in execution order."""
    topup: list[Window] = []
    first: list[Window] = []
    backfill: list[Window] = []
    for p in sorted(pairs, key=lambda s: (s.exchange, s.ticker, s.source)):
        if p.status == "not_found" and not (full_refresh or retry_not_found):
            continue
        end = p.target_end(to_date)
        if end < p.fetch_from:
            continue
        if full_refresh or p.coverage_from is None or p.coverage_through is None:
            chunks = chunk_down(end, p.fetch_from)
            first.append(Window(p.ticker, p.exchange, *chunks[0], "first", p.source))
            backfill += [
                Window(p.ticker, p.exchange, s, e, "backfill", p.source)
                for s, e in chunks[1:]
            ]
            continue
        if p.coverage_through < end:
            start = max(p.fetch_from, p.coverage_through - timedelta(days=OVERLAP_DAYS))
            topup += [
                Window(p.ticker, p.exchange, s, e, "topup", p.source)
                for s, e in chunk_up(start, end)
            ]
        if not p.history_done():
            newest = p.coverage_from - timedelta(days=1)
            backfill += [
                Window(p.ticker, p.exchange, s, e, "backfill", p.source)
                for s, e in chunk_down(newest, p.fetch_from)
            ]
    backfill.sort(key=lambda w: (-w.end.toordinal(), w.exchange, w.ticker))
    return topup + first + backfill


def summarize_plan(windows: list[Window], *, max_units: int) -> dict[str, Any]:
    by_phase: dict[str, int] = {}
    for w in windows:
        by_phase[w.phase] = by_phase.get(w.phase, 0) + 1
    units = len(windows) * UNITS_PER_REQUEST
    per_run = max_units if max_units > 0 else units
    return {
        "pairs": len({w.key for w in windows}),
        "aliases": len({w.key for w in windows if w.source}),
        "windows": len(windows),
        "by_phase": by_phase,
        "units": units,
        "units_this_run": min(units, per_run),
        "runs_to_finish": (0 if units == 0 else -(-units // per_run)),
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
def recount(
    pairs: dict[tuple[str, str, str], PairState],
    bars_dir: Path,
    ticker: str,
    exchange: str,
    *,
    to_date: date,
) -> None:
    """Refresh ``n_rows`` of a stored pair's state rows.

    An alias row counts the stored bars inside its span; the pair's own row
    counts the rest, so the rows of one pair sum to its stored bars.
    """
    rows = [p for p in pairs.values() if (p.ticker, p.exchange) == (ticker, exchange)]
    aliased = 0
    for p in rows:
        if p.source:
            p.n_rows = count_rows_between(
                bars_dir, ticker, exchange, p.fetch_from, p.target_end(to_date)
            )
            aliased += p.n_rows
    for p in rows:
        if not p.source:
            p.n_rows = max(0, count_rows(bars_dir, ticker, exchange) - aliased)
        p.settle_status(to_date)


class AuthError(RuntimeError):
    """The API rejected the key (HTTP 401/403): no point continuing."""


@dataclass
class RunResult:
    requests: int = 0
    units: int = 0
    ok: int = 0
    failed: int = 0
    new_rows: int = 0
    budget_stopped: bool = False
    time_stopped: bool = False
    touched: set[tuple[str, str]] = field(default_factory=set)


def _window_params(window: Window) -> dict[str, Any]:
    start = datetime.combine(window.start, time(0), tzinfo=timezone.utc)
    end = datetime.combine(
        window.end + timedelta(days=1), time(0), tzinfo=timezone.utc
    ) - timedelta(seconds=1)
    return {
        "interval": INTERVAL,
        "from": int(start.timestamp()),
        "to": int(end.timestamp()),
        "fmt": "json",
    }


def _upgrade_audit(path: Path) -> None:
    """Rewrite an audit file whose header predates a column (adds it empty)."""
    if not path.exists():
        return
    with open(path, newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle), [])
    if header == AUDIT_COLUMNS or not header:
        return
    old = pd.read_csv(path, dtype=str, keep_default_na=False)
    for col in AUDIT_COLUMNS:
        if col not in old.columns:
            old[col] = ""
    _atomic.to_csv(old[AUDIT_COLUMNS], path, index=False)


def _append_audit(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=AUDIT_COLUMNS)
        if new:
            writer.writeheader()
        writer.writerow({c: row.get(c, "") for c in AUDIT_COLUMNS})


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def execution_rounds(windows: list[Window]) -> list[list[Window]]:
    """Group the plan into rounds of at most one window per pair.

    Stage 1 (top-ups and first windows) comes before stage 2 (history), so
    every pair is current before any history is filled. Within a stage, round
    k holds each stored pair's k-th window in plan order (history newest
    first), so a pair's windows -- its own and its aliases', which write the
    same files -- always run one after another and extend each span
    contiguously, while different pairs can run in parallel.
    """
    rounds: list[list[Window]] = []
    for stage in (
        [w for w in windows if w.phase != "backfill"],
        [w for w in windows if w.phase == "backfill"],
    ):
        queues: dict[tuple[str, str], list[Window]] = {}
        for w in stage:
            queues.setdefault(w.pair, []).append(w)
        depth = max((len(q) for q in queues.values()), default=0)
        for k in range(depth):
            rounds.append([q[k] for q in queues.values() if len(q) > k])
    return rounds


def execute(
    windows: list[Window],
    pairs: dict[tuple[str, str], PairState],
    *,
    session: Any,
    bars_dir: Path,
    state_path: Path,
    audit_path: Path,
    previous_state: pd.DataFrame,
    to_date: date,
    max_units: int = 0,
    max_minutes: float = 0,
    workers: int = 1,
    get: Callable[..., Any] | None = None,
    clock: Callable[[], float] | None = None,
) -> RunResult:
    """Run the plan in :func:`execution_rounds`, persisting state after every request.

    Up to ``workers`` pairs are fetched at once (one window per pair at a
    time). Stops before a request that would exceed ``max_units`` or once
    ``max_minutes`` of wall clock have passed (0 = no limit): a window is
    never cut in half, and the next run resumes from the saved state.
    """
    import threading
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    tick = clock or _time.monotonic
    deadline = tick() + max_minutes * 60 if max_minutes > 0 else None
    if get is None:
        from _http import get_with_retry as get  # type: ignore[no-redef]
    result = RunResult()
    skip: set[tuple[str, str]] = set()
    streak: dict[tuple[str, str], int] = {}
    lock = threading.Lock()
    auth_error: list[AuthError] = []
    _upgrade_audit(audit_path)

    def save_state() -> None:
        _atomic.to_csv(state_frame(pairs, previous_state), state_path, index=False)

    def run_one(window: Window) -> None:
        requested_at = _now()  # when the request starts, not when it was queued
        p = pairs[window.key]
        url = f"{BASE_URL}/{p.symbol}.{window.exchange}"
        audit: dict[str, Any] = {
            "requested_at": requested_at,
            "ticker": window.ticker,
            "exchange": window.exchange,
            "phase": window.phase,
            "from_date": window.start.isoformat(),
            "to_date": window.end.isoformat(),
            "units": UNITS_PER_REQUEST,
            "source": window.source,
        }
        error = ""
        payload: Any = None
        try:
            response = get(
                session,
                url,
                params=_window_params(window),
                timeout=REQUEST_TIMEOUT,
                log=log,
                label=url,
            )
            audit["http_status"] = response.status_code
            if response.status_code in (401, 403):
                raise AuthError(
                    f"HTTP {response.status_code} from the intraday endpoint (check EODHD_API_KEY / plan)"
                )
            if response.status_code == 404:
                error = "HTTP 404"
            elif response.status_code != 200:
                error = f"HTTP {response.status_code}"
            else:
                payload = response.json()
                if not isinstance(payload, list):
                    error = f"unexpected payload: {str(payload)[:160]}"
        except AuthError as exc:
            with lock:
                auth_error.append(exc)
                _append_audit(audit_path, {**audit, "error": "auth"})
            return
        except (
            Exception
        ) as exc:  # noqa: BLE001 - one pair's failure must not end the run
            error = f"{type(exc).__name__}: {exc}"[:200]
        bars = pd.DataFrame(columns=BAR_COLUMNS)
        added = 0
        if not error:
            try:
                bars = bars_from_payload(payload, window.ticker, window.exchange)
                added = merge_bars(
                    bars_dir,
                    bars,
                    replace=(window.start, window.end) if window.source else None,
                )
            except (
                Exception
            ) as exc:  # noqa: BLE001 - a bad payload marks the pair, not the run
                error = f"{type(exc).__name__}: {exc}"[:200]
        with lock:
            p.fetched_at = requested_at
            if error:
                result.failed += 1
                if error == "HTTP 404":
                    p.status, p.last_error = "not_found", error
                elif p.status != "not_found":
                    p.status, p.last_error = "error", error
                skip.add(window.key)
                _append_audit(audit_path, {**audit, "error": error, "rows": 0})
                save_state()
                return
            result.ok += 1
            result.new_rows += added
            if p.status in {"error", "not_found"}:
                p.status, p.last_error = "", ""
            p.coverage_from = min(filter(None, [p.coverage_from, window.start]))
            p.coverage_through = max(filter(None, [p.coverage_through, window.end]))
            if not bars.empty:
                streak[window.key] = 0
                first, last = date.fromisoformat(
                    bars["date"].min()
                ), date.fromisoformat(bars["date"].max())
                p.first_data_date = min(filter(None, [p.first_data_date, first]))
                p.latest_data_date = max(filter(None, [p.latest_data_date, last]))
            elif window.phase in {"backfill", "first"} and (
                p.first_data_date is None or window.end < p.first_data_date
            ):
                streak[window.key] = streak.get(window.key, 0) + 1
                if streak[window.key] >= EMPTY_STREAK_STOP:
                    p.coverage_from = min(p.coverage_from, p.fetch_from)
            p.settle_status(to_date)
            _append_audit(audit_path, {**audit, "rows": len(bars)})
            save_state()

    stop = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for round_windows in execution_rounds(windows):
            batch: list[Window] = []
            for window in round_windows:
                p = pairs[window.key]
                if window.key in skip or (
                    window.phase == "backfill" and p.history_done()
                ):
                    continue  # failed earlier, or history start reached by the empty-streak rule
                if max_units > 0 and result.units + UNITS_PER_REQUEST > max_units:
                    result.budget_stopped = stop = True
                    break
                if deadline is not None and tick() >= deadline:
                    result.time_stopped = stop = True
                    break
                result.requests += 1
                result.units += UNITS_PER_REQUEST
                result.touched.add(window.key)
                batch.append(window)
            futures = [pool.submit(run_one, w) for w in batch]
            for future in futures:
                future.result()
            if auth_error:
                break
            if stop:
                break
    for pair in {key[:2] for key in result.touched}:
        recount(pairs, bars_dir, *pair, to_date=to_date)
    save_state()
    if auth_error:
        raise auth_error[0]
    return result


# --------------------------------------------------------------------------- #
# targets and CLI
# --------------------------------------------------------------------------- #
def parse_spec(spec: str) -> tuple[str, str]:
    text = spec.strip()
    if "." not in text:
        return text.upper(), "US"
    ticker, exchange = text.rsplit(".", 1)
    return ticker.upper(), exchange.upper()


@dataclass(frozen=True)
class Span:
    """A hand-kept correction for one pair (``[[alias]]`` / ``[[mask]]`` in the toml)."""

    ticker: str
    exchange: str
    start: date | None  # inclusive; None = open
    end: date | None  # inclusive; None = open
    source: str = ""  # alias: the symbol EODHD files the bars under
    reason: str = ""


def _spans(config: dict[str, Any], section: str) -> list[Span]:
    out = []
    for item in config.get(section, []):
        out.append(
            Span(
                ticker=str(item["ticker"]).upper(),
                exchange=str(item.get("exchange", "US")).upper(),
                start=_d(item.get("from")),
                end=_d(item.get("until")),
                source=str(item.get("source", "")).upper(),
                reason=str(item.get("reason", "")),
            )
        )
    return out


def aliases(config: dict[str, Any]) -> list[Span]:
    return _spans(config, "alias")


def masks(config: dict[str, Any]) -> list[Span]:
    return _spans(config, "mask")


def apply_corrections(targets: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    """Targets with masks applied and alias rows added (``source`` column).

    A mask moves the pair's ``fetch_from`` past the masked span (masks start
    at the beginning of history), dropping pairs left with nothing to fetch.
    An alias adds a row for the same stored pair over the overlap of the
    pair's span and the alias's ``from .. until``.
    """
    out = targets.copy()
    if "source" not in out.columns:
        out["source"] = ""
    for m in masks(config):
        if m.end is None:
            continue
        hit = (out["ticker"] == m.ticker) & (out["exchange"] == m.exchange)
        floor = (m.end + timedelta(days=1)).isoformat()
        out.loc[hit, "fetch_from"] = out.loc[hit, "fetch_from"].map(
            lambda v: max(str(v), floor)
        )
        to = out["fetch_to"].fillna("").astype(str)
        out = out[~(hit & (to != "") & (to < out["fetch_from"]))]
    rows = []
    for a in aliases(config):
        owner = out[
            (out["ticker"] == a.ticker)
            & (out["exchange"] == a.exchange)
            & (out["source"] == "")
        ]
        for r in owner.to_dict("records"):
            start = max(_d(r["fetch_from"]), a.start or date.min)
            end_owner = _d(r.get("fetch_to"))
            end = (
                min(d for d in (end_owner, a.end) if d)
                if (end_owner or a.end)
                else None
            )
            if end is not None and end < start:
                continue
            rows.append(
                {
                    **r,
                    "source": a.source,
                    "fetch_from": start.isoformat(),
                    "fetch_to": end.isoformat() if end else "",
                }
            )
    if rows:
        out = pd.concat([out, pd.DataFrame(rows)], ignore_index=True)
    return out.reset_index(drop=True)


def purge_masked(
    bars_dir: Path, state: pd.DataFrame, config: dict[str, Any]
) -> tuple[pd.DataFrame, int]:
    """Delete stored bars inside masked spans; fix the affected state rows.

    Local and idempotent (a pair with nothing masked left is not rewritten).
    Returns the updated state and the number of bars deleted.
    """
    state = state.copy()
    deleted = 0
    for m in masks(config):
        start = m.start or date(1990, 1, 1)
        end = m.end or date.today()
        gone = drop_span(bars_dir, m.ticker, m.exchange, start, end)
        if not gone:
            continue
        deleted += gone
        own = (state["ticker"] == m.ticker) & (state["exchange"] == m.exchange)
        own &= state["source"].map(_source) == ""
        files = pair_files(bars_dir, m.ticker, m.exchange)
        dates = (
            pd.concat([pd.read_parquet(f, columns=["date"]) for f in files])["date"]
            if files
            else pd.Series(dtype=str)
        )
        state.loc[own, "n_rows"] = str(count_rows(bars_dir, m.ticker, m.exchange))
        state.loc[own, "first_data_date"] = dates.min() if len(dates) else ""
        state.loc[own, "latest_data_date"] = dates.max() if len(dates) else ""
    return state, deleted


def load_targets(
    membership_path: Path,
    config: dict[str, Any],
    *,
    tickers: list[str],
    limit: int,
) -> pd.DataFrame:
    rules = config["rules"]
    history_start = date.fromisoformat(rules["history_start"])
    if not membership_path.exists():
        raise FileNotFoundError(
            f"{membership_path} not found -- build the universe first: "
            "python eodhd/build_intraday_universe.py (refresh intraday runs it)"
        )
    targets = universe.fetch_targets(
        universe.load_membership(membership_path),
        history_start=history_start,
        warmup_days=int(rules["warmup_days"]),
    )
    if tickers:
        wanted = [parse_spec(t) for t in tickers]
        known = {(r.ticker, r.exchange) for r in targets.itertuples(index=False)}
        extra = [
            {
                "ticker": t,
                "exchange": e,
                "fetch_from": history_start.isoformat(),
                "fetch_to": "",
                "current": True,
            }
            for t, e in wanted
            if (t, e) not in known
        ]
        targets = pd.concat([targets, pd.DataFrame(extra)], ignore_index=True)
        keys = set(wanted)
        targets = targets[
            [(t, e) in keys for t, e in zip(targets["ticker"], targets["exchange"])]
        ]
    if limit:
        targets = targets.head(limit)
    return apply_corrections(targets.reset_index(drop=True), config)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD 1m bars for the intraday lane."
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Only these pairs (TICKER.EXCH; bare = .US).",
    )
    parser.add_argument(
        "--to", default="", help="Upper bound YYYY-MM-DD (default: today, UTC)."
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Only the first N target pairs."
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Refetch every window (state is ignored, files merged).",
    )
    parser.add_argument(
        "--max-units",
        type=int,
        default=0,
        help="Stop before exceeding N API units (0 = uncapped).",
    )
    parser.add_argument(
        "--max-minutes",
        type=float,
        default=0,
        help="Stop starting new requests after N minutes of wall clock (0 = no limit).",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Pairs fetched at once (default {DEFAULT_WORKERS}).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="Print the plan and units; no API calls."
    )
    parser.add_argument(
        "--retag-sessions",
        action="store_true",
        help="Re-derive session tags of stored bars on early-close days, then exit (local).",
    )
    parser.add_argument(
        "--volume-fix",
        action="store_true",
        help="Rebuild the US volume fix from the stored bars and daily volume, then exit (local).",
    )
    return parser.parse_args(argv)


def rebuild_volume_fix() -> None:
    """Rebuild ``bars_1m_volume_*.parquet``; a failure is reported, not fatal."""
    import intraday_volume

    try:
        days, _fix = intraday_volume.build(EODHD_RAW_ROOT)
    except Exception as exc:  # noqa: BLE001 - derived data; the bars are safe
        logging.warning("volume fix not rebuilt: %s: %s", type(exc).__name__, exc)
        return
    print(intraday_volume.summary(days))


def print_plan(summary: dict[str, Any], *, max_units: int, to_date: date) -> None:
    phases = (
        ", ".join(f"{k}={v}" for k, v in sorted(summary["by_phase"].items())) or "none"
    )
    cap = f"cap {max_units:,} units/run" if max_units > 0 else "uncapped"
    alias = (
        f" (of which {summary['aliases']} aliases)" if summary.get("aliases") else ""
    )
    print(
        f"intraday bars_1m plan to {to_date}: {summary['pairs']} pairs{alias}, {summary['windows']} windows ({phases}); "
        f"{summary['units']:,} units in total, {summary['units_this_run']:,} this run ({cap}); "
        f"runs to finish: {summary['runs_to_finish']}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
    if args.retag_sessions:
        files, changed = retag_sessions(BARS_DIR)
        print(f"retagged {changed:,} bars in {files:,} files (early-close sessions)")
        return 0
    if args.volume_fix:
        rebuild_volume_fix()
        return 0
    to_date = (
        date.fromisoformat(args.to) if args.to else datetime.now(timezone.utc).date()
    )
    config = universe.load_config()
    try:
        targets = load_targets(
            universe.MEMBERSHIP_PATH, config, tickers=args.tickers, limit=args.limit
        )
    except FileNotFoundError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    previous = load_state(STATE_PATH)
    if not args.dry_run:
        previous, purged = purge_masked(BARS_DIR, previous, config)
        if purged:
            _atomic.to_csv(previous, STATE_PATH, index=False)
            print(
                f"masked: deleted {purged:,} bars of another security ([[mask]] in the toml)"
            )
    pairs = merge_targets(targets, previous)
    windows = plan_windows(
        pairs.values(),
        to_date=to_date,
        full_refresh=args.full_refresh,
        retry_not_found=bool(args.tickers),
    )
    print_plan(
        summarize_plan(windows, max_units=args.max_units),
        max_units=args.max_units,
        to_date=to_date,
    )
    if args.dry_run:
        return 0
    if not windows:
        rebuild_volume_fix()
        return 0
    import requests
    from fetch_eodhd_us_fundamentals import _get_api_key

    session = requests.Session()
    session.params = {"api_token": _get_api_key()}
    try:
        result = execute(
            windows,
            pairs,
            session=session,
            bars_dir=BARS_DIR,
            state_path=STATE_PATH,
            audit_path=AUDIT_PATH,
            previous_state=previous,
            to_date=to_date,
            max_units=args.max_units,
            max_minutes=args.max_minutes,
            workers=args.workers,
        )
    except AuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(
        f"done: {result.requests} requests ({result.units:,} units), {result.ok} ok, {result.failed} failed, "
        f"{result.new_rows:,} new bars across {len(result.touched)} pairs"
        + (
            "; stopped at the unit cap (history continues next run)"
            if result.budget_stopped
            else ""
        )
        + (
            "; stopped at the time limit (history continues next run)"
            if result.time_stopped
            else ""
        )
    )
    rebuild_volume_fix()
    return 1 if result.requests and not result.ok else 0


if __name__ == "__main__":
    sys.exit(main())
