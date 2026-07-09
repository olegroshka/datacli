"""Shared refresh logic for the EODHD fundamentals fetchers.

The US and UK/EU fundamentals fetchers are append-only by default (they skip any
firm already on disk). This module adds the pieces needed to turn them into
proper incremental refreshers, kept as pure, side-effect-free helpers so they can
be unit-tested against fixtures without any live API call:

- target selection (`select_targets`) — which firms to (re)fetch given a mode,
- the per-firm freshness sidecar (`fundamentals_fetch_state.csv`) load/build/write,
- the earnings-calendar lookup used to target only firms that reported.

See ``FUNDAMENTALS_REFRESH_DESIGN.md`` for the full design.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

Pair = tuple[str, str]

MODE_BACKFILL = "backfill"  # legacy: only firms not yet present
MODE_UPDATE = "update"  # reported/stale firms + new firms
MODE_FULL = "full"  # every firm
MODES = (MODE_BACKFILL, MODE_UPDATE, MODE_FULL)

STATE_FILENAME = "fundamentals_fetch_state.csv"
STATE_COLUMNS = [
    "ticker",
    "exchange",
    "status",
    "fetched_at",
    "latest_filing_date",
    "latest_statement_date",
    "n_quarters",
    "detail",
]

EODHD_CALENDAR_URL = "https://eodhd.com/api/calendar/earnings"


# --------------------------------------------------------------------------- #
# identity helpers
# --------------------------------------------------------------------------- #
def normalize_pair(ticker: str, exchange: str) -> Pair:
    """Uppercase/strip a (ticker, exchange) pair for consistent set membership."""
    return (str(ticker).strip().upper(), str(exchange).strip().upper())


def parse_calendar_code(code: str) -> Pair | None:
    """Parse an EODHD ``TICKER.EXCHANGE`` calendar code into a normalized pair."""
    if not code or "." not in code:
        return None
    ticker, exchange = str(code).rsplit(".", 1)
    if not ticker or not exchange:
        return None
    return normalize_pair(ticker, exchange)


def _utcnow_naive() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)


def _to_naive_utc(value: Any) -> pd.Timestamp | None:
    ts = pd.to_datetime(value, errors="coerce", utc=True)
    if pd.isna(ts):
        return None
    return ts.tz_localize(None)


def _date_series(frame: pd.DataFrame, column: str) -> pd.Series:
    """Coerce a frame column to datetimes, or an all-NaT series if it's absent."""
    if column in frame.columns:
        return pd.to_datetime(frame[column], errors="coerce")
    return pd.Series(pd.NaT, index=frame.index)


# --------------------------------------------------------------------------- #
# sidecar I/O
# --------------------------------------------------------------------------- #
def load_state(path: Path) -> dict[Pair, dict[str, Any]]:
    """Load the fundamentals state sidecar into a keyed dict.

    Returns an empty dict when the file does not exist. ``fetched_at`` is parsed
    to a naive-UTC ``pd.Timestamp`` (or ``None``); other columns are passed
    through as-is.
    """
    if not Path(path).exists():
        return {}
    frame = pd.read_csv(path)
    state: dict[Pair, dict[str, Any]] = {}
    for raw in frame.to_dict("records"):
        row: dict[str, Any] = {str(k): v for k, v in raw.items()}
        key = normalize_pair(row.get("ticker", ""), row.get("exchange", ""))
        row["fetched_at"] = _to_naive_utc(row.get("fetched_at"))
        state[key] = row
    return state


def summarize_panel(panel: pd.DataFrame) -> dict[Pair, dict[str, Any]]:
    """Per-firm summary from the merged quarterly panel.

    For each ``(ticker, exchange)``: latest ``filing_date``, latest statement
    ``date``, and the number of distinct statement dates (``n_quarters``).
    """
    if panel is None or panel.empty:
        return {}
    frame = panel.copy()
    frame["ticker"] = frame["ticker"].astype(str).str.strip().str.upper()
    frame["exchange"] = frame["exchange"].astype(str).str.strip().str.upper()
    filing = _date_series(frame, "filing_date")
    statement = _date_series(frame, "date")
    frame = frame.assign(_filing=filing, _statement=statement)

    summary: dict[Pair, dict[str, Any]] = {}
    for (ticker, exchange), group in frame.groupby(["ticker", "exchange"], sort=False):
        f_max = group["_filing"].max()
        s_max = group["_statement"].max()
        summary[(ticker, exchange)] = {
            "latest_filing_date": None if pd.isna(f_max) else f_max.date().isoformat(),
            "latest_statement_date": (
                None if pd.isna(s_max) else s_max.date().isoformat()
            ),
            "n_quarters": int(group["_statement"].dropna().nunique()),
        }
    return summary


def build_state_rows(
    panel: pd.DataFrame,
    *,
    prior: Mapping[Pair, dict[str, Any]] | None,
    refreshed: Iterable[Pair],
    empty: Iterable[Pair] = (),
    now: pd.Timestamp | None = None,
) -> list[dict[str, Any]]:
    """Build sidecar rows from the merged panel + this run's fetch results.

    Args:
        panel: The merged ``fundamentals_quarterly`` frame after the run.
        prior: Previously-recorded state (keeps ``fetched_at`` for firms not
            refreshed this run).
        refreshed: Firms successfully (re)fetched this run — stamped with ``now``.
        empty: Firms the provider returned nothing for — recorded ``status=empty``.
        now: Fetch timestamp (defaults to current UTC).

    Returns:
        One row dict per firm, sorted by (ticker, exchange).
    """
    now = now if now is not None else _utcnow_naive()
    prior = dict(prior or {})
    refreshed_set = {normalize_pair(*p) for p in refreshed}
    empty_set = {normalize_pair(*p) for p in empty}
    summary = summarize_panel(panel)

    firms = set(summary) | set(prior) | refreshed_set | empty_set
    rows: list[dict[str, Any]] = []
    for firm in sorted(firms):
        summ = summary.get(firm, {})
        prev = prior.get(firm, {})
        was_refreshed = firm in refreshed_set or firm in empty_set
        fetched_at = now if was_refreshed else prev.get("fetched_at")
        if firm in summary:
            status = "ok"
        elif firm in empty_set:
            status = "empty"
        else:
            status = prev.get("status", "ok")
        rows.append(
            {
                "ticker": firm[0],
                "exchange": firm[1],
                "status": status,
                "fetched_at": None if fetched_at is None else _fmt_ts(fetched_at),
                "latest_filing_date": summ.get("latest_filing_date")
                or prev.get("latest_filing_date"),
                "latest_statement_date": summ.get("latest_statement_date")
                or prev.get("latest_statement_date"),
                "n_quarters": summ.get("n_quarters", prev.get("n_quarters", 0)),
                "detail": prev.get("detail", ""),
            }
        )
    return rows


def _fmt_ts(value: Any) -> str:
    ts = pd.Timestamp(value)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def write_state(
    path: Path,
    panel: pd.DataFrame,
    *,
    refreshed: Iterable[Pair],
    empty: Iterable[Pair] = (),
    now: pd.Timestamp | None = None,
) -> None:
    """Merge this run's results into the sidecar and write it to ``path``."""
    prior = load_state(path)
    rows = build_state_rows(
        panel, prior=prior, refreshed=refreshed, empty=empty, now=now
    )
    pd.DataFrame(rows, columns=STATE_COLUMNS).to_csv(path, index=False)


# --------------------------------------------------------------------------- #
# target selection
# --------------------------------------------------------------------------- #
def is_stale(
    firm: Pair,
    state: Mapping[Pair, dict[str, Any]] | None,
    *,
    as_of: pd.Timestamp,
    stale_days: int,
) -> bool:
    """A firm is stale if it has no state row or was last fetched > N days ago."""
    if not state:
        return True
    row = state.get(normalize_pair(*firm))
    if not row:
        return True
    fetched = row.get("fetched_at")
    fetched_ts = (
        _to_naive_utc(fetched) if not isinstance(fetched, pd.Timestamp) else fetched
    )
    if fetched_ts is None:
        return True
    return fetched_ts < (as_of - pd.Timedelta(days=stale_days))


def select_targets(
    candidates: Sequence[Pair],
    *,
    mode: str,
    present: Iterable[Pair],
    state: Mapping[Pair, dict[str, Any]] | None = None,
    reported: Iterable[Pair] | None = None,
    stale_days: int = 30,
    as_of: pd.Timestamp | None = None,
) -> list[Pair]:
    """Decide which firms to (re)fetch, in candidate order.

    ``full`` -> all; ``backfill`` -> only firms not present; ``update`` -> new
    firms plus present firms that either reported (when ``reported`` is provided)
    or are stale by ``stale_days`` (otherwise). Pure and deterministic.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode!r}")
    candidates = [normalize_pair(*c) for c in candidates]
    present_set = {normalize_pair(*p) for p in present}

    if mode == MODE_FULL:
        return list(candidates)

    new_firms = [c for c in candidates if c not in present_set]
    if mode == MODE_BACKFILL:
        return new_firms

    as_of = as_of if as_of is not None else _utcnow_naive()
    reported_set = (
        {normalize_pair(*r) for r in reported} if reported is not None else None
    )
    refresh_present: list[Pair] = []
    for firm in candidates:
        if firm not in present_set:
            continue
        if reported_set is not None:
            if firm in reported_set:
                refresh_present.append(firm)
        elif is_stale(firm, state, as_of=as_of, stale_days=stale_days):
            refresh_present.append(firm)

    target = set(new_firms) | set(refresh_present)
    return [c for c in candidates if c in target]


def resolve_reported_from(
    reported_from: str | None,
    panel: pd.DataFrame | None,
    *,
    as_of: pd.Timestamp,
    default_lookback_days: int = 60,
) -> str:
    """Start date for the earnings-calendar window (``YYYY-MM-DD``).

    Precedence: an explicit ``reported_from`` override, else the panel's latest
    ``filing_date`` (how current our fundamentals are — a report dated after this
    is something we may not have), else ``as_of`` minus ``default_lookback_days``.

    Deliberately *not* keyed off the sidecar's ``fetched_at``: that would narrow
    the window after any partial update and risk missing reports.
    """
    if reported_from:
        return reported_from
    if panel is not None and not panel.empty and "filing_date" in panel.columns:
        filed = pd.to_datetime(panel["filing_date"], errors="coerce").max()
        if pd.notna(filed):
            return pd.Timestamp(filed).date().isoformat()
    return (as_of - pd.Timedelta(days=default_lookback_days)).date().isoformat()


# --------------------------------------------------------------------------- #
# earnings calendar (live)
# --------------------------------------------------------------------------- #
def fetch_reported_symbols(
    session: Any,
    from_date: str,
    to_date: str,
    *,
    url: str = EODHD_CALENDAR_URL,
    timeout: int = 60,
) -> set[Pair] | None:
    """Return firms that reported earnings in ``[from_date, to_date]``.

    Uses EODHD ``/calendar/earnings``. Returns a set of ``(ticker, exchange)``
    pairs, or ``None`` if the endpoint is unavailable/errors (caller should fall
    back to stale-days targeting). ``session`` is a ``requests.Session`` already
    carrying the API token.
    """
    try:
        resp = session.get(
            url,
            params={"from": from_date, "to": to_date, "fmt": "json"},
            timeout=timeout,
        )
    except Exception:
        return None
    if resp.status_code != 200:
        return None
    try:
        payload = resp.json()
    except Exception:
        return None
    rows = payload.get("earnings") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return None
    reported: set[Pair] = set()
    for row in rows:
        code = row.get("code") if isinstance(row, dict) else None
        pair = parse_calendar_code(code) if code else None
        if pair is not None:
            reported.add(pair)
    return reported
