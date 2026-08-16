"""Fetch daily prices for qualifying US common stocks from EODHD.

Uses the completed US common-stock fundamentals coverage summary under
`data/raw/eodhd/us_common/coverage_summary.csv` and applies the same
incremental tail-refresh semantics already proven in the UK/EU workflow.

Usage:
    uv run python eodhd/fetch_eodhd_us_prices.py
    uv run python eodhd/fetch_eodhd_us_prices.py --tickers AAPL.US MSFT.US

Outputs:
    data/raw/eodhd/us_common/prices_daily.parquet
    data/raw/eodhd/us_common/prices_fetch_state.csv
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import requests
import _atomic
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_eu_prices import (
    DEFAULT_FROM,
    EODHD_BASE,
    FLUSH_EVERY,
    HTTP_TIMEOUT,
    _max_iso_date,
    build_fetch_state_lookup,
    build_resume_state,
    choose_fetch_window,
    compute_next_resume_from,
    merge_fetch_state_rows,
    merge_price_frames,
    normalize_iso_date,
)
from fetch_eodhd_us_fundamentals import _ROOT, _get_api_key, parse_ticker_spec

RAW_DIR = EODHD_RAW_ROOT / "us_common"
COVERAGE_PATH = RAW_DIR / "coverage_summary.csv"
PRICES_PATH = RAW_DIR / "prices_daily.parquet"
PRICES_STATE_PATH = RAW_DIR / "prices_fetch_state.csv"

DELAY = 0.12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_prices")


def load_target_tickers(
    *, explicit_specs: list[str], limit: int
) -> list[tuple[str, str]]:
    if explicit_specs:
        tickers = [parse_ticker_spec(value) for value in explicit_specs]
    else:
        if not COVERAGE_PATH.exists():
            raise RuntimeError(
                f"Run fetch_eodhd_us_fundamentals.py first — {COVERAGE_PATH} not found"
            )
        cov = pd.read_csv(COVERAGE_PATH)
        qualifying = cov[cov["both_60q"] == 1][["ticker", "exchange"]].drop_duplicates()
        tickers = [tuple(row) for row in qualifying.itertuples(index=False, name=None)]
    if limit > 0:
        tickers = tickers[:limit]
    return tickers


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD US common-stock prices into the data root"
    )
    parser.add_argument(
        "--tickers", nargs="*", default=[], help="Explicit TICKER.EXCHANGE identifiers"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of target firms"
    )
    parser.add_argument(
        "--from",
        dest="from_date",
        default=DEFAULT_FROM,
        help="History start date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="History end date (YYYY-MM-DD); defaults to current UTC date",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=5,
        help="Overlap days when refreshing cached tickers to absorb provider corrections",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Ignore cached last-date state and refetch full requested history for targeted tickers",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = _get_api_key()

    qualifying = load_target_tickers(explicit_specs=args.tickers, limit=args.limit)
    log.info("Qualifying US common-stock firms for price pull: %d", len(qualifying))

    existing: pd.DataFrame | None = None
    if PRICES_PATH.exists():
        existing = pd.read_parquet(PRICES_PATH)
        log.info(
            "Cached US price rows on disk: %d (%d firms)",
            len(existing),
            existing[["ticker", "exchange"]].drop_duplicates().shape[0],
        )
    resume_state = build_resume_state(existing)
    existing_state = (
        pd.read_csv(PRICES_STATE_PATH) if PRICES_STATE_PATH.exists() else None
    )
    state_lookup = build_fetch_state_lookup(existing_state)

    session = requests.Session()
    session.params = {"api_token": api_key}
    new_frames: list[pd.DataFrame] = []
    new_state_rows: list[dict[str, object]] = []
    attempted = 0
    fetched = 0
    empty = 0
    skipped = 0
    full_history = 0
    incremental = 0

    def _flush() -> None:
        nonlocal existing, existing_state
        if not new_frames:
            if new_state_rows:
                merged_state = merge_fetch_state_rows(existing_state, new_state_rows)
                PRICES_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
                _atomic.to_csv(merged_state, PRICES_STATE_PATH, index=False)
                existing_state = merged_state
                new_state_rows.clear()
            return
        new_df = pd.concat(new_frames, ignore_index=True)
        merged = merge_price_frames(existing, new_df)
        PRICES_PATH.parent.mkdir(parents=True, exist_ok=True)
        _atomic.to_parquet(merged, PRICES_PATH, index=False)
        existing = merged
        if new_state_rows:
            merged_state = merge_fetch_state_rows(existing_state, new_state_rows)
            PRICES_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(merged_state, PRICES_STATE_PATH, index=False)
            existing_state = merged_state
        log.info(
            "  [FLUSH] %d total US price rows on disk (%d firms)",
            len(merged),
            merged[["ticker", "exchange"]].drop_duplicates().shape[0],
        )
        new_frames.clear()
        new_state_rows.clear()

    for i, (ticker, exchange) in enumerate(qualifying):
        prior_state = state_lookup.get((ticker, exchange), {})
        prior_last_date = normalize_iso_date(
            resume_state.get((ticker, exchange), prior_state.get("latest_data_date"))
        )
        prior_coverage = normalize_iso_date(prior_state.get("coverage_through"))
        fetch_window = choose_fetch_window(
            last_date=prior_last_date,
            coverage_through=prior_coverage,
            from_date=args.from_date,
            to_date=args.to_date,
            overlap_days=args.overlap_days,
            full_refresh=args.full_refresh,
        )
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if fetch_window is None:
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "up_to_date",
                    "mode": "skip",
                    "query_from": None,
                    "query_to": args.to_date,
                    "coverage_through": prior_coverage or args.to_date,
                    "previous_data_date": prior_last_date,
                    "latest_data_date": prior_last_date,
                    "response_rows": 0,
                    "response_min_date": None,
                    "response_max_date": None,
                    "overlap_days": args.overlap_days,
                    "next_resume_from": compute_next_resume_from(
                        latest_data_date=prior_last_date,
                        coverage_through=prior_coverage or args.to_date,
                        from_date=args.from_date,
                        overlap_days=args.overlap_days,
                    ),
                    "fetched_at": fetched_at,
                    "detail": "",
                }
            )
            continue

        fetch_from, fetch_to = fetch_window
        attempted += 1
        if prior_last_date is None or args.full_refresh:
            full_history += 1
        else:
            incremental += 1

        if attempted > 0 and attempted % 50 == 0:
            log.info(
                "Progress: %d/%d (attempted=%d, fetched=%d, skipped=%d, full=%d, incremental=%d)",
                i + 1,
                len(qualifying),
                attempted,
                fetched,
                skipped,
                full_history,
                incremental,
            )

        url = f"{EODHD_BASE}/eod/{ticker}.{exchange}"
        try:
            response = session.get(
                url,
                params={
                    "from": fetch_from,
                    "to": fetch_to,
                    "period": "d",
                    "fmt": "json",
                },
                timeout=HTTP_TIMEOUT,
            )
        except requests.RequestException:
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "request_error",
                    "mode": (
                        "full"
                        if prior_last_date is None or args.full_refresh
                        else "incremental"
                    ),
                    "query_from": fetch_from,
                    "query_to": fetch_to,
                    "coverage_through": prior_coverage,
                    "previous_data_date": prior_last_date,
                    "latest_data_date": prior_last_date,
                    "response_rows": 0,
                    "response_min_date": None,
                    "response_max_date": None,
                    "overlap_days": args.overlap_days,
                    "next_resume_from": compute_next_resume_from(
                        latest_data_date=prior_last_date,
                        coverage_through=prior_coverage,
                        from_date=args.from_date,
                        overlap_days=args.overlap_days,
                    ),
                    "fetched_at": fetched_at,
                    "detail": "request_error",
                }
            )
            time.sleep(DELAY)
            continue

        if response.status_code != 200:
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": f"http_{response.status_code}",
                    "mode": (
                        "full"
                        if prior_last_date is None or args.full_refresh
                        else "incremental"
                    ),
                    "query_from": fetch_from,
                    "query_to": fetch_to,
                    "coverage_through": prior_coverage,
                    "previous_data_date": prior_last_date,
                    "latest_data_date": prior_last_date,
                    "response_rows": 0,
                    "response_min_date": None,
                    "response_max_date": None,
                    "overlap_days": args.overlap_days,
                    "next_resume_from": compute_next_resume_from(
                        latest_data_date=prior_last_date,
                        coverage_through=prior_coverage,
                        from_date=args.from_date,
                        overlap_days=args.overlap_days,
                    ),
                    "fetched_at": fetched_at,
                    "detail": response.text[:200],
                }
            )
            time.sleep(DELAY)
            continue

        try:
            data = response.json()
        except Exception:
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "decode_error",
                    "mode": (
                        "full"
                        if prior_last_date is None or args.full_refresh
                        else "incremental"
                    ),
                    "query_from": fetch_from,
                    "query_to": fetch_to,
                    "coverage_through": prior_coverage,
                    "previous_data_date": prior_last_date,
                    "latest_data_date": prior_last_date,
                    "response_rows": 0,
                    "response_min_date": None,
                    "response_max_date": None,
                    "overlap_days": args.overlap_days,
                    "next_resume_from": compute_next_resume_from(
                        latest_data_date=prior_last_date,
                        coverage_through=prior_coverage,
                        from_date=args.from_date,
                        overlap_days=args.overlap_days,
                    ),
                    "fetched_at": fetched_at,
                    "detail": "decode_error",
                }
            )
            continue

        if not data or not isinstance(data, list):
            empty += 1
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "empty",
                    "mode": (
                        "full"
                        if prior_last_date is None or args.full_refresh
                        else "incremental"
                    ),
                    "query_from": fetch_from,
                    "query_to": fetch_to,
                    "coverage_through": fetch_to,
                    "previous_data_date": prior_last_date,
                    "latest_data_date": prior_last_date,
                    "response_rows": 0,
                    "response_min_date": None,
                    "response_max_date": None,
                    "overlap_days": args.overlap_days,
                    "next_resume_from": compute_next_resume_from(
                        latest_data_date=prior_last_date,
                        coverage_through=fetch_to,
                        from_date=args.from_date,
                        overlap_days=args.overlap_days,
                    ),
                    "fetched_at": fetched_at,
                    "detail": "",
                }
            )
            time.sleep(DELAY)
            continue

        df = pd.DataFrame(data)
        df["ticker"] = ticker
        df["exchange"] = exchange
        new_frames.append(df)
        response_min_date = normalize_iso_date(df["date"].min())
        response_max_date = normalize_iso_date(df["date"].max())
        latest_date = _max_iso_date(prior_last_date, response_max_date)
        if latest_date is not None:
            resume_state[(ticker, exchange)] = latest_date
        new_state_rows.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "status": "ok",
                "mode": (
                    "full"
                    if prior_last_date is None or args.full_refresh
                    else "incremental"
                ),
                "query_from": fetch_from,
                "query_to": fetch_to,
                "coverage_through": fetch_to,
                "previous_data_date": prior_last_date,
                "latest_data_date": latest_date,
                "response_rows": len(df),
                "response_min_date": response_min_date,
                "response_max_date": response_max_date,
                "overlap_days": args.overlap_days,
                "next_resume_from": compute_next_resume_from(
                    latest_data_date=latest_date,
                    coverage_through=fetch_to,
                    from_date=args.from_date,
                    overlap_days=args.overlap_days,
                ),
                "fetched_at": fetched_at,
                "detail": "",
            }
        )
        fetched += 1

        if attempted % FLUSH_EVERY == 0:
            _flush()

        time.sleep(DELAY)

    _flush()
    log.info(
        "Done: attempted=%d, fetched=%d, empty=%d, skipped=%d, full=%d, incremental=%d",
        attempted,
        fetched,
        empty,
        skipped,
        full_history,
        incremental,
    )

    if PRICES_PATH.exists():
        final = pd.read_parquet(PRICES_PATH)
        print(
            f"\nUS prices: {len(final):,} rows, "
            f"{final[['ticker', 'exchange']].drop_duplicates().shape[0]} firms"
        )
        print(f"Date range: {final['date'].min()} -> {final['date'].max()}")


if __name__ == "__main__":
    main()
