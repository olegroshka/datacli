"""Bulk end-of-day refresh -- the fast path.

Instead of one HTTP request per ticker (tens of thousands, hours of wall-clock),
this pulls a whole exchange's latest trading day(s) in a single call via EODHD
``/eod-bulk-last-day/{EXCHANGE}`` and merges the new bars/events into the existing
parquets, advancing the same state sidecars the per-ticker fetchers use.

It is registry-driven: it iterates the lanes in ``eodhd_datasets.py``, derives the
set of exchanges from each lane's state sidecar, and shares one bulk cache across
lanes (so ``us_common`` and ``us_etf`` share the single ``US`` pull).

Scope / caveat:
- Handles prices, dividends, splits (the state-backed, per-ticker-today datasets).
  Fundamentals is refreshed separately by its own ``--update`` mode.
- Bulk appends new bars with the provider's *current* adjustment; it does NOT
  re-adjust historical ``adjusted_close`` after a split. Tickers that split on a
  fetched day are reported so they can get a periodic per-ticker ``--full-refresh``.
- A pair whose gap to today exceeds ``--days`` is skipped (never hole-punched) and
  reported; run the normal per-ticker refresh for those.

Usage:
    uv run python eodhd/fetch_eodhd_bulk.py --run
    uv run python eodhd/fetch_eodhd_bulk.py us_common us_etf --kinds prices --run
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests  # type: ignore[import-untyped]

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eodhd_datasets import LANES, DatasetSpec, LaneConfig  # noqa: E402
from fetch_eodhd_eu_fundamentals import _get_api_key  # noqa: E402

BULK_URL = "https://eodhd.com/api/eod-bulk-last-day"
HTTP_TIMEOUT = 120
BULK_KINDS = ("prices", "dividends", "splits")
KEY_COLS = ("ticker", "exchange")
# Identity keys: a row is "already present" iff it matches on ALL of these.
# Events include the value because a firm can have multiple distinct dividends
# (special + regular) or splits on the same ex-date -- keying on ex_date alone
# would collapse them and lose data.
OUTPUT_KEYS = {
    "prices": ["ticker", "exchange", "date"],
    "dividends": ["ticker", "exchange", "ex_date", "dividend", "unadjusted_dividend"],
    "splits": ["ticker", "exchange", "ex_date", "split_factor"],
}
Pair = tuple[str, str]

PRICE_COLUMNS = [
    "date",
    "open",
    "high",
    "low",
    "close",
    "adjusted_close",
    "volume",
    "ticker",
    "exchange",
]
DIVIDEND_COLUMNS = [
    "ticker",
    "exchange",
    "ex_date",
    "declaration_date",
    "record_date",
    "payment_date",
    "period",
    "dividend",
    "unadjusted_dividend",
    "currency",
]
SPLIT_COLUMNS = [
    "ticker",
    "exchange",
    "ex_date",
    "split_factor",
    "numerator",
    "denominator",
    "split_ratio",
]


# --------------------------------------------------------------------------- #
# pure parsing / normalization
# --------------------------------------------------------------------------- #
def parse_split(value: Any) -> tuple[float | None, float | None, float | None]:
    """Parse an EODHD split string ``"10.000000/1.000000"`` -> (num, den, ratio)."""
    if not value or "/" not in str(value):
        return None, None, None
    try:
        num_s, den_s = str(value).split("/", 1)
        num, den = float(num_s), float(den_s)
    except ValueError:
        return None, None, None
    ratio = num / den if den else None
    return num, den, ratio


def _num(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def _freshness_series(state: pd.DataFrame, col: str) -> pd.Series:
    """The lane's 'how far do we have data' column as datetimes (NaT if absent).

    Keyed off the dataset's ``freshness_col`` -- ``latest_data_date`` for prices
    (the actual last bar) and ``coverage_through`` for events. Using the real
    last-bar date matters: the per-ticker fetchers set ``coverage_through`` to
    the query ceiling, which can run *ahead* of the last bar when a fetch happens
    before that day's close -- keying the gap off it would then skip the day.
    """
    if col in state.columns:
        return pd.to_datetime(state[col], errors="coerce")
    return pd.Series(pd.NaT, index=state.index)


def normalize_prices(rows: list[dict]) -> pd.DataFrame:
    """Bulk price rows -> the prices_daily schema."""
    out = []
    for r in rows:
        out.append(
            {
                "date": r.get("date"),
                "open": _num(r.get("open")),
                "high": _num(r.get("high")),
                "low": _num(r.get("low")),
                "close": _num(r.get("close")),
                "adjusted_close": _num(r.get("adjusted_close")),
                "volume": _num(r.get("volume")),
                "ticker": str(r.get("code", "")).strip().upper(),
                "exchange": str(r.get("exchange_short_name", "")).strip().upper(),
            }
        )
    return pd.DataFrame(out, columns=PRICE_COLUMNS)


def normalize_dividends(rows: list[dict]) -> pd.DataFrame:
    """Bulk dividend rows -> the dividends_history schema."""
    out = []
    for r in rows:
        out.append(
            {
                "ticker": str(r.get("code", "")).strip().upper(),
                "exchange": str(r.get("exchange", "")).strip().upper(),
                "ex_date": r.get("date"),
                "declaration_date": r.get("declarationDate"),
                "record_date": r.get("recordDate"),
                "payment_date": r.get("paymentDate"),
                "period": r.get("period"),
                "dividend": _num(r.get("dividend")),
                "unadjusted_dividend": _num(r.get("unadjustedValue")),
                "currency": r.get("currency"),
            }
        )
    return pd.DataFrame(out, columns=DIVIDEND_COLUMNS)


def normalize_splits(rows: list[dict]) -> pd.DataFrame:
    """Bulk split rows -> the splits_history schema."""
    out = []
    for r in rows:
        num, den, ratio = parse_split(r.get("split"))
        out.append(
            {
                "ticker": str(r.get("code", "")).strip().upper(),
                "exchange": str(r.get("exchange", "")).strip().upper(),
                "ex_date": r.get("date"),
                "split_factor": r.get("split"),
                "numerator": num,
                "denominator": den,
                "split_ratio": ratio,
            }
        )
    return pd.DataFrame(out, columns=SPLIT_COLUMNS)


NORMALIZERS = {
    "prices": normalize_prices,
    "dividends": normalize_dividends,
    "splits": normalize_splits,
}
DATE_COL = {"prices": "date", "dividends": "ex_date", "splits": "ex_date"}


# --------------------------------------------------------------------------- #
# pure gap / merge / state logic
# --------------------------------------------------------------------------- #
def gap_dates(
    state: pd.DataFrame,
    as_of: pd.Timestamp,
    max_days: int,
    *,
    freshness_col: str = "coverage_through",
) -> list[str]:
    """Business days to bulk-fetch: from the oldest last-data date (+1), capped
    at ``max_days`` most-recent, through ``as_of`` inclusive."""
    cov = _freshness_series(state, freshness_col)
    min_cov = cov.min()
    floor = as_of.normalize() - pd.Timedelta(days=max_days - 1)
    if pd.isna(min_cov):
        start = floor
    else:
        start = max(min_cov.normalize() + pd.Timedelta(days=1), floor)
    if start > as_of.normalize():
        return []
    return [d.date().isoformat() for d in pd.bdate_range(start, as_of.normalize())]


def coverage_map(
    state: pd.DataFrame, *, freshness_col: str = "coverage_through"
) -> dict[Pair, pd.Timestamp]:
    """(ticker, exchange) -> last-data timestamp (NaT-safe), per ``freshness_col``."""
    cov = _freshness_series(state, freshness_col)
    result: dict[Pair, pd.Timestamp] = {}
    for ticker, exchange, c in zip(state["ticker"], state["exchange"], cov):
        result[(str(ticker).upper(), str(exchange).upper())] = c
    return result


def select_new_rows(
    normalized: pd.DataFrame,
    coverage: dict[Pair, pd.Timestamp],
    *,
    date_col: str,
    earliest_fetched: pd.Timestamp,
) -> tuple[pd.DataFrame, set[Pair]]:
    """Keep only in-universe rows dated after each pair's coverage.

    Returns (new_rows, skipped_pairs). A pair whose coverage predates
    ``earliest_fetched`` would leave a hole, so its rows are dropped and the pair
    is reported as skipped (needs a per-ticker refresh).
    """
    if normalized.empty:
        return normalized, set()
    frame = normalized.copy()
    frame[date_col] = pd.to_datetime(frame[date_col], errors="coerce")
    keep_idx = []
    skipped: set[Pair] = set()
    for idx, ticker, exchange, dt in zip(
        frame.index, frame["ticker"], frame["exchange"], frame[date_col]
    ):
        pair = (str(ticker).upper(), str(exchange).upper())
        if pair not in coverage:
            continue  # not in our universe
        cov = coverage[pair]
        if pd.notna(cov) and cov < earliest_fetched - pd.Timedelta(days=1):
            skipped.add(pair)
            continue  # gap too large -> would hole-punch; leave for per-ticker
        if pd.isna(dt):
            continue
        if pd.isna(cov) or dt > cov:
            keep_idx.append(idx)
    new_rows = frame.loc[keep_idx].copy()
    new_rows[date_col] = new_rows[date_col].dt.date.astype(str)
    return new_rows, skipped


def merge_output(
    existing: pd.DataFrame, new: pd.DataFrame, keys: list[str]
) -> pd.DataFrame:
    """Append genuinely-new rows; never remove or rewrite existing rows.

    A new row is added only if its identity key (``keys``) is not already present
    in ``existing``. Existing rows are left completely untouched, so this is safe
    for datasets with legitimate multiple rows per (ticker, ex_date) and is fully
    idempotent.
    """
    if new is None or new.empty:
        return existing
    new_dedup = new.drop_duplicates(subset=keys, keep="last")
    if existing is None or existing.empty:
        return new_dedup.reset_index(drop=True)
    aligned = new_dedup[existing.columns.intersection(new_dedup.columns)]
    existing_keys = set(map(tuple, existing[keys].astype(str).to_numpy()))
    is_new = [
        tuple(k) not in existing_keys for k in aligned[keys].astype(str).to_numpy()
    ]
    to_add = aligned[is_new]
    if to_add.empty:
        return existing
    return pd.concat([existing, to_add], ignore_index=True)


def advance_state(
    state: pd.DataFrame,
    *,
    new_max_by_pair: dict[Pair, str],
    now: str,
) -> pd.DataFrame:
    """Advance state only for pairs that received new data.

    ``coverage_through`` and ``latest_data_date`` are set to the pair's *actual*
    newest bar/event date -- never to ``as_of``. Advancing past real data would
    make the next run skip a not-yet-posted day (e.g. today's close before the
    market closes). Pairs with no new data are left untouched, so the next run's
    gap window re-scans them (one cheap bulk call) with no risk of a hole.
    """
    s = state.reset_index(drop=True).copy()
    for col in (
        "coverage_through",
        "latest_data_date",
        "previous_data_date",
        "fetched_at",
        "status",
        "mode",
    ):
        if col not in s.columns:
            s[col] = pd.NA
    for i in range(len(s)):
        pair = (str(s.at[i, "ticker"]).upper(), str(s.at[i, "exchange"]).upper())
        if pair not in new_max_by_pair:
            continue
        s.at[i, "previous_data_date"] = s.at[i, "latest_data_date"]
        s.at[i, "latest_data_date"] = new_max_by_pair[pair]
        s.at[i, "coverage_through"] = new_max_by_pair[pair]
        s.at[i, "fetched_at"] = now
        s.at[i, "status"] = "ok"
        s.at[i, "mode"] = "bulk"
    return s


# --------------------------------------------------------------------------- #
# live IO
# --------------------------------------------------------------------------- #
def bulk_fetch(
    session: requests.Session,
    exchange: str,
    date: str,
    kind: str,
    cache: dict[tuple[str, str, str], list[dict]],
) -> list[dict]:
    """One /eod-bulk-last-day call (cached per exchange/date/kind)."""
    key = (exchange, date, kind)
    if key in cache:
        return cache[key]
    params: dict[str, str] = {"fmt": "json", "date": date}
    if kind in ("dividends", "splits"):
        params["type"] = kind
    try:
        resp = session.get(
            f"{BULK_URL}/{exchange}", params=params, timeout=HTTP_TIMEOUT
        )
    except requests.RequestException:
        cache[key] = []
        return []
    rows = resp.json() if resp.status_code == 200 else []
    if not isinstance(rows, list):
        rows = []
    cache[key] = rows
    return rows


def _exchanges(state: pd.DataFrame) -> list[str]:
    return sorted({str(e).strip().upper() for e in state["exchange"].dropna().unique()})


def process_dataset(
    lane: LaneConfig,
    ds: DatasetSpec,
    *,
    session: requests.Session,
    cache: dict[tuple[str, str, str], list[dict]],
    as_of: pd.Timestamp,
    max_days: int,
    now: str,
    dry_run: bool,
) -> dict[str, Any]:
    """Bulk-refresh a single (lane, dataset). Returns a summary dict."""
    root = lane.resolved_root()
    state_path = root / ds.state  # type: ignore[operator]
    out_path = root / ds.output
    summary = {
        "lane": lane.name,
        "kind": ds.kind,
        "new_rows": 0,
        "pairs_advanced": 0,
        "skipped": 0,
        "splits_seen": 0,
    }
    if not state_path.exists():
        summary["note"] = "no state sidecar"
        return summary

    state = pd.read_csv(state_path)
    dates = gap_dates(state, as_of, max_days, freshness_col=ds.freshness_col)
    if not dates:
        summary["note"] = "up to date"
        return summary
    earliest = pd.Timestamp(dates[0])
    date_col = DATE_COL[ds.kind]

    frames = []
    for exchange in _exchanges(state):
        for date in dates:
            rows = bulk_fetch(session, exchange, date, ds.kind, cache)
            if rows:
                frames.append(NORMALIZERS[ds.kind](rows))
    normalized = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    cov = coverage_map(state, freshness_col=ds.freshness_col)
    new_rows, skipped = select_new_rows(
        normalized, cov, date_col=date_col, earliest_fetched=earliest
    )
    summary["skipped"] = len(skipped)
    if ds.kind == "splits" and not new_rows.empty:
        summary["splits_seen"] = int(
            new_rows[["ticker", "exchange"]].drop_duplicates().shape[0]
        )

    new_max_by_pair: dict[Pair, str] = {}
    if not new_rows.empty:
        for (ticker, exchange), grp in new_rows.groupby(
            ["ticker", "exchange"], sort=False
        ):
            new_max_by_pair[(str(ticker).upper(), str(exchange).upper())] = str(
                grp[date_col].max()
            )
    summary["new_rows"] = int(len(new_rows))
    summary["pairs_advanced"] = len(new_max_by_pair)
    summary["dates_fetched"] = f"{dates[0]}..{dates[-1]} ({len(dates)}d)"
    if dry_run or new_rows.empty:
        return summary

    existing = pd.read_parquet(out_path) if out_path.exists() else pd.DataFrame()
    merged = merge_output(existing, new_rows, OUTPUT_KEYS[ds.kind])
    merged.to_parquet(out_path, index=False)
    updated_state = advance_state(state, new_max_by_pair=new_max_by_pair, now=now)
    updated_state.to_csv(state_path, index=False)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Bulk end-of-day fast refresh (prices/dividends/splits)"
    )
    p.add_argument(
        "lanes", nargs="*", help=f"Lanes (default all). Choices: {', '.join(LANES)}"
    )
    p.add_argument(
        "--kinds",
        default=",".join(BULK_KINDS),
        help=f"Comma list (default: {','.join(BULK_KINDS)})",
    )
    p.add_argument(
        "--days", type=int, default=7, help="Max days back to fill via bulk (default 7)"
    )
    p.add_argument(
        "--as-of",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Upper date bound (YYYY-MM-DD; default today UTC)",
    )
    p.add_argument("--run", action="store_true", help="Execute (default: dry-run)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    lane_names = args.lanes or list(LANES)
    bad = [n for n in lane_names if n not in LANES]
    if bad:
        print(f"unknown lane(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    as_of = pd.Timestamp(args.as_of)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    session = requests.Session()
    session.params = {"api_token": _get_api_key()}  # type: ignore[assignment]
    cache: dict[tuple[str, str, str], list[dict]] = {}

    mode = "RUN" if args.run else "DRY-RUN"
    print(
        f"Bulk fast refresh ({mode}) — as-of {as_of.date()}, up to {args.days}d back\n"
    )
    total_new = total_skipped = 0
    split_tickers: list[str] = []
    for lane_name in lane_names:
        lane = LANES[lane_name]
        for ds in lane.datasets:
            if ds.kind not in kinds or ds.kind not in BULK_KINDS or ds.state is None:
                continue
            s = process_dataset(
                lane,
                ds,
                session=session,
                cache=cache,
                as_of=as_of,
                max_days=args.days,
                now=now,
                dry_run=not args.run,
            )
            total_new += s["new_rows"]
            total_skipped += s["skipped"]
            note = f"  [{s['note']}]" if s.get("note") else ""
            extra = f", splits_seen={s['splits_seen']}" if s.get("splits_seen") else ""
            print(
                f"  {s['lane']:<16} {s['kind']:<10} new_rows={s['new_rows']:<6} "
                f"advanced={s['pairs_advanced']:<5} skipped={s['skipped']:<4}"
                f"{extra}{note}"
            )

    print(
        f"\nbulk calls: {len(cache)}  |  new rows: {total_new}  |  skipped pairs (need per-ticker): {total_skipped}"
    )
    if not args.run:
        print("Dry-run only. Re-run with --run to write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
