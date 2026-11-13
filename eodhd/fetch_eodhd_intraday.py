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

Targets come from ``universe_membership.parquet`` (``build_intraday_universe.py``).
Design: docs/INTRADAY_1M_DESIGN.md, section 6.

Usage:
    uv run python eodhd/fetch_eodhd_intraday.py --dry-run          # plan + units, spends nothing
    uv run python eodhd/fetch_eodhd_intraday.py --max-units 25000  # what `refresh intraday --run` does
    uv run python eodhd/fetch_eodhd_intraday.py --tickers NVDA.US MU.US

Outputs (under <data_root>/intraday/):
    bars_1m/<TICKER>.<EXCH>_<YYYY>.parquet
    bars_1m_fetch_state.csv
    bars_1m_fetch_audit.csv
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
]

#: exchange -> (timezone, regular-session open, regular-session close); ``None``
#: open means a 24h market (every bar is ``24h``). Unknown exchanges are 24h UTC.
SESSION_RULES: dict[str, tuple[str, time | None, time | None]] = {
    "US": ("America/New_York", time(9, 30), time(16, 0)),
    "INDX": ("America/New_York", time(9, 30), time(16, 0)),
    "LSE": ("Europe/London", time(8, 0), time(16, 30)),
    "FOREX": ("UTC", None, None),
}


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
    tz, rth_open, rth_close = SESSION_RULES.get(exchange, ("UTC", None, None))
    local = ts.dt.tz_convert(tz)
    if rth_open is None:
        session = pd.Series("24h", index=raw.index)
    else:
        clock = local.dt.time
        session = pd.Series("post", index=raw.index)
        session[clock < rth_open] = "pre"
        session[(clock >= rth_open) & (clock < rth_close)] = "rth"
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


def merge_bars(bars_dir: Path, bars: pd.DataFrame) -> int:
    """Merge bars into their ticker-year files (last copy of a ``ts`` wins).

    Returns the number of genuinely new bar timestamps.
    """
    if bars.empty:
        return 0
    added = 0
    ticker, exchange = str(bars["ticker"].iloc[0]), str(bars["exchange"].iloc[0])
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

    @property
    def key(self) -> tuple[str, str]:
        return (self.ticker, self.exchange)

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
        }


def merge_targets(
    targets: pd.DataFrame, state: pd.DataFrame
) -> dict[tuple[str, str], PairState]:
    """One :class:`PairState` per target pair, carrying over the saved state.

    State rows for pairs no longer targeted are kept (history is never
    dropped) but are not fetched.
    """
    saved = {(r["ticker"], r["exchange"]): r for r in state.to_dict("records")}
    pairs: dict[tuple[str, str], PairState] = {}
    for r in targets.to_dict("records"):
        key = (str(r["ticker"]), str(r["exchange"]))
        old = saved.get(key, {})
        pairs[key] = PairState(
            ticker=key[0],
            exchange=key[1],
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
    pairs: dict[tuple[str, str], PairState], previous: pd.DataFrame
) -> pd.DataFrame:
    rows = {p.key: p.row() for p in pairs.values()}
    for r in previous.to_dict("records"):
        rows.setdefault(
            (r["ticker"], r["exchange"]), {c: str(r.get(c, "")) for c in STATE_COLUMNS}
        )
    frame = pd.DataFrame(list(rows.values()), columns=STATE_COLUMNS)
    return frame.sort_values(["exchange", "ticker"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Window:
    ticker: str
    exchange: str
    start: date  # inclusive, UTC date
    end: date  # inclusive, UTC date
    phase: str  # topup | first | backfill

    @property
    def key(self) -> tuple[str, str]:
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
    for p in sorted(pairs, key=lambda s: (s.exchange, s.ticker)):
        if p.status == "not_found" and not (full_refresh or retry_not_found):
            continue
        end = p.target_end(to_date)
        if end < p.fetch_from:
            continue
        if full_refresh or p.coverage_from is None or p.coverage_through is None:
            chunks = chunk_down(end, p.fetch_from)
            first.append(Window(p.ticker, p.exchange, *chunks[0], "first"))
            backfill += [
                Window(p.ticker, p.exchange, s, e, "backfill") for s, e in chunks[1:]
            ]
            continue
        if p.coverage_through < end:
            start = max(p.fetch_from, p.coverage_through - timedelta(days=OVERLAP_DAYS))
            topup += [
                Window(p.ticker, p.exchange, s, e, "topup")
                for s, e in chunk_up(start, end)
            ]
        if not p.history_done():
            newest = p.coverage_from - timedelta(days=1)
            backfill += [
                Window(p.ticker, p.exchange, s, e, "backfill")
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
        "windows": len(windows),
        "by_phase": by_phase,
        "units": units,
        "units_this_run": min(units, per_run),
        "runs_to_finish": (0 if units == 0 else -(-units // per_run)),
    }


# --------------------------------------------------------------------------- #
# execution
# --------------------------------------------------------------------------- #
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
    k holds each pair's k-th window in plan order (history newest first), so
    a pair's windows always run one after another and extend its span
    contiguously, while different pairs can run in parallel.
    """
    rounds: list[list[Window]] = []
    for stage in (
        [w for w in windows if w.phase != "backfill"],
        [w for w in windows if w.phase == "backfill"],
    ):
        queues: dict[tuple[str, str], list[Window]] = {}
        for w in stage:
            queues.setdefault(w.key, []).append(w)
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

    def save_state() -> None:
        _atomic.to_csv(state_frame(pairs, previous_state), state_path, index=False)

    def run_one(window: Window) -> None:
        requested_at = _now()  # when the request starts, not when it was queued
        p = pairs[window.key]
        url = f"{BASE_URL}/{window.ticker}.{window.exchange}"
        audit: dict[str, Any] = {
            "requested_at": requested_at,
            "ticker": window.ticker,
            "exchange": window.exchange,
            "phase": window.phase,
            "from_date": window.start.isoformat(),
            "to_date": window.end.isoformat(),
            "units": UNITS_PER_REQUEST,
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
                added = merge_bars(bars_dir, bars)
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
    for key in result.touched:
        pairs[key].n_rows = count_rows(bars_dir, *key)
        pairs[key].settle_status(to_date)
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
    return targets.reset_index(drop=True)


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
    return parser.parse_args(argv)


def print_plan(summary: dict[str, Any], *, max_units: int, to_date: date) -> None:
    phases = (
        ", ".join(f"{k}={v}" for k, v in sorted(summary["by_phase"].items())) or "none"
    )
    cap = f"cap {max_units:,} units/run" if max_units > 0 else "uncapped"
    print(
        f"intraday bars_1m plan to {to_date}: {summary['pairs']} pairs, {summary['windows']} windows ({phases}); "
        f"{summary['units']:,} units in total, {summary['units_this_run']:,} this run ({cap}); "
        f"runs to finish: {summary['runs_to_finish']}"
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    args = parse_args(argv)
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
    if args.dry_run or not windows:
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
    return 1 if result.requests and not result.ok else 0


if __name__ == "__main__":
    sys.exit(main())
