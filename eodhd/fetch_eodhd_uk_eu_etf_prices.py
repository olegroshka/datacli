"""Fetch daily prices for the UK/EU ETF universe from EODHD."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests
from fetch_eodhd_eu_fundamentals import _get_api_key
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
from fetch_eodhd_uk_eu_etf_universe import (
    ETF_TICKERS_PATH,
    RAW_DIR,
    load_target_tickers,
)

PRICES_PATH = RAW_DIR / "prices_daily.parquet"
PRICES_STATE_PATH = RAW_DIR / "prices_fetch_state.csv"
DELAY = 0.12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_uk_eu_etf_prices")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD UK/EU ETF prices into btest"
    )
    parser.add_argument(
        "--tickers", nargs="*", default=[], help="Explicit TICKER.EXCHANGE identifiers"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of target ETFs"
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
    parser.add_argument("--overlap-days", type=int, default=5)
    parser.add_argument("--full-refresh", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    api_key = _get_api_key()
    targets = load_target_tickers(
        explicit_specs=args.tickers, limit=args.limit, provider_path=ETF_TICKERS_PATH
    )
    log.info("UK/EU ETF target count: %d", len(targets))

    existing: pd.DataFrame | None = (
        pd.read_parquet(PRICES_PATH) if PRICES_PATH.exists() else None
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
    attempted = fetched = skipped = 0

    def _flush() -> None:
        nonlocal existing, existing_state
        if new_frames:
            merged = merge_price_frames(
                existing, pd.concat(new_frames, ignore_index=True)
            )
            PRICES_PATH.parent.mkdir(parents=True, exist_ok=True)
            merged.to_parquet(PRICES_PATH, index=False)
            existing = merged
        if new_state_rows:
            merged_state = merge_fetch_state_rows(existing_state, new_state_rows)
            PRICES_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            merged_state.to_csv(PRICES_STATE_PATH, index=False)
            existing_state = merged_state
        if new_frames or new_state_rows:
            rows = 0 if existing is None or existing.empty else len(existing)
            firms = (
                0
                if existing is None or existing.empty
                else existing[["ticker", "exchange"]].drop_duplicates().shape[0]
            )
            log.info("  [FLUSH] UK/EU ETF prices=%d rows (%d ETFs)", rows, firms)
        new_frames.clear()
        new_state_rows.clear()

    for i, (ticker, exchange) in enumerate(targets):
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
        if attempted % 50 == 0:
            log.info(
                "Progress: %d/%d (attempted=%d, fetched=%d, skipped=%d)",
                i + 1,
                len(targets),
                attempted,
                fetched,
                skipped,
            )

        response = session.get(
            f"{EODHD_BASE}/eod/{ticker}.{exchange}",
            params={"from": fetch_from, "to": fetch_to, "period": "d", "fmt": "json"},
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            skipped += 1
            time.sleep(DELAY)
            continue
        data = response.json()
        if not data or not isinstance(data, list):
            skipped += 1
            time.sleep(DELAY)
            continue
        df = pd.DataFrame(data)
        df["ticker"] = ticker
        df["exchange"] = exchange
        new_frames.append(df)
        response_min_date = normalize_iso_date(df["date"].min())
        response_max_date = normalize_iso_date(df["date"].max())
        latest_date = _max_iso_date(prior_last_date, response_max_date)
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
    log.info("Done: attempted=%d, fetched=%d, skipped=%d", attempted, fetched, skipped)


if __name__ == "__main__":
    main()
