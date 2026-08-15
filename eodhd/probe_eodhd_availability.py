"""Probe EODHD daily-price availability and quality for ad-hoc tickers.

Unlike the lane fetchers (which ingest into the qualifying-universe parquet),
this is a read-only diagnostic: it pulls full EOD history for each requested
ticker from the **paid** EODHD API and reports how far back data goes plus basic
quality metrics. It never touches the lane outputs; the raw JSON payloads are
cached under ``<data_root>/probe_cache`` by default so repeat probes are free
(``--raw-cache-dir ""`` disables the cache, ``--refresh`` bypasses it).

Usage:
    uv run python eodhd/probe_eodhd_availability.py \
        CRWD DDOG DASH PLTR PYPL SHOP ZS APP ARM BKR CEG GEHC SNDK WDAY
    uv run python eodhd/probe_eodhd_availability.py AAPL.US --from 1990-01-01
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # noqa: E402
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_eu_fundamentals import _get_api_key  # noqa: E402

EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 60
DELAY = 0.12
DEFAULT_FROM = "1990-01-01"
DEFAULT_EXCHANGE = "US"

_ROOT = Path(__file__).resolve().parents[2]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog=os.environ.get("DATACLI_PROG") or None,
        description="Probe EODHD daily-price availability/quality for ad-hoc "
        "tickers. Hits the paid EODHD API (needs EODHD_API_KEY) and caches the "
        "raw JSON under <data_root>/probe_cache by default "
        f"({EODHD_RAW_ROOT / 'probe_cache'}; --raw-cache-dir \"\" disables). "
        "Read-only: never touches the lane outputs.",
    )
    p.add_argument(
        "tickers", nargs="+", help="Tickers, bare (CRWD) or TICKER.EXCHANGE (CRWD.US)"
    )
    p.add_argument(
        "--from",
        dest="from_date",
        default=DEFAULT_FROM,
        help="History start (YYYY-MM-DD)",
    )
    p.add_argument(
        "--to",
        dest="to_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="History end (YYYY-MM-DD); defaults to today (UTC)",
    )
    p.add_argument(
        "--exchange", default=DEFAULT_EXCHANGE, help="Default exchange for bare tickers"
    )
    p.add_argument(
        "--raw-cache-dir",
        default=str(EODHD_RAW_ROOT / "probe_cache"),
        help="Where to store raw JSON payloads (set empty to disable caching)",
    )
    p.add_argument(
        "--refresh", action="store_true", help="Ignore raw cache and refetch"
    )
    p.add_argument(
        "--out-csv", default="", help="Optional path to write the summary table as CSV"
    )
    return p.parse_args()


def split_spec(value: str, default_exchange: str) -> tuple[str, str]:
    if "." in value:
        ticker, exchange = value.rsplit(".", 1)
        return ticker.strip().upper(), exchange.strip().upper()
    return value.strip().upper(), default_exchange.strip().upper()


def cache_path(cache_dir: Path, ticker: str, exchange: str) -> Path:
    return cache_dir / exchange / f"{ticker}.json.gz"


def fetch_eod(
    session: requests.Session,
    ticker: str,
    exchange: str,
    from_date: str,
    to_date: str,
    cache_dir: Path | None,
    refresh: bool,
) -> tuple[list[dict] | None, str]:
    """Return (rows, status). status is 'cache', 'ok', 'empty', or 'http_<code>'/error."""
    if cache_dir is not None and not refresh:
        cp = cache_path(cache_dir, ticker, exchange)
        if cp.exists():
            try:
                with gzip.open(cp, "rt", encoding="utf-8") as fh:
                    return json.load(fh), "cache"
            except Exception:
                pass

    url = f"{EODHD_BASE}/eod/{ticker}.{exchange}"
    try:
        resp = session.get(
            url,
            params={"from": from_date, "to": to_date, "period": "d", "fmt": "json"},
            timeout=HTTP_TIMEOUT,
        )
    except requests.RequestException as exc:
        return None, f"request_error:{exc.__class__.__name__}"
    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"
    try:
        data = resp.json()
    except Exception:
        return None, "decode_error"
    if not isinstance(data, list) or not data:
        return [], "empty"
    if cache_dir is not None:
        cp = cache_path(cache_dir, ticker, exchange)
        cp.parent.mkdir(parents=True, exist_ok=True)
        with gzip.open(cp, "wt", encoding="utf-8") as fh:
            json.dump(data, fh)
    return data, "ok"


def analyze(rows: list[dict], to_date: str) -> dict:
    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date").reset_index(drop=True)
    n = len(df)
    first, last = df["date"].iloc[0], df["date"].iloc[-1]

    # Expected NYSE-ish trading days via business-day calendar (approx; ignores holidays).
    bdays = pd.bdate_range(first, last)
    density = n / max(len(bdays), 1)

    # Largest gap between consecutive observations, in calendar days.
    gaps = df["date"].diff().dt.days.dropna()
    max_gap = int(gaps.max()) if not gaps.empty else 0
    n_gaps_gt5 = int((gaps > 5).sum())

    close = pd.to_numeric(df.get("close"), errors="coerce")
    adj = pd.to_numeric(df.get("adjusted_close"), errors="coerce")
    vol = pd.to_numeric(df.get("volume"), errors="coerce")

    nan_close = int(close.isna().sum())
    nonpos_close = int((close <= 0).sum())
    zero_vol = int((vol == 0).sum())
    has_adj = "adjusted_close" in df.columns and adj.notna().any()
    dup_dates = int(df["date"].duplicated().sum())

    # Staleness: business days between last observation and the requested 'to'.
    to_ts = pd.Timestamp(to_date)
    stale_bdays = max(len(pd.bdate_range(last, to_ts)) - 1, 0)

    return {
        "first_date": first.date().isoformat(),
        "last_date": last.date().isoformat(),
        "rows": n,
        "exp_bdays": len(bdays),
        "density_%": round(density * 100, 1),
        "max_gap_days": max_gap,
        "gaps_gt5": n_gaps_gt5,
        "stale_bdays": stale_bdays,
        "nan_close": nan_close,
        "nonpos_close": nonpos_close,
        "zero_vol_days": zero_vol,
        "has_adj_close": has_adj,
        "dup_dates": dup_dates,
    }


def main() -> None:
    args = parse_args()
    api_key = _get_api_key()
    cache_dir = Path(args.raw_cache_dir) if args.raw_cache_dir else None

    session = requests.Session()
    session.params = {"api_token": api_key}  # type: ignore[assignment]

    records: list[dict] = []
    for spec in args.tickers:
        ticker, exchange = split_spec(spec, args.exchange)
        rows, status = fetch_eod(
            session,
            ticker,
            exchange,
            args.from_date,
            args.to_date,
            cache_dir,
            args.refresh,
        )
        rec: dict = {"ticker": ticker, "exchange": exchange, "status": status}
        if rows:
            rec.update(analyze(rows, args.to_date))
        records.append(rec)
        if status not in ("cache",):
            time.sleep(DELAY)

    cols = [
        "ticker",
        "exchange",
        "status",
        "first_date",
        "last_date",
        "rows",
        "exp_bdays",
        "density_%",
        "max_gap_days",
        "gaps_gt5",
        "stale_bdays",
        "nan_close",
        "nonpos_close",
        "zero_vol_days",
        "has_adj_close",
        "dup_dates",
    ]
    out = pd.DataFrame(records)
    for c in cols:
        if c not in out.columns:
            out[c] = pd.NA
    out = out[cols].sort_values("first_date", na_position="last")

    console = _render.make_console()
    console.print(_render.df_table(out, title=f"EODHD probe ({len(out)} ticker(s))"))

    if args.out_csv:
        out.to_csv(args.out_csv, index=False)
        console.print(f"[dim]Wrote summary -> {args.out_csv}[/dim]")


if __name__ == "__main__":
    main()
