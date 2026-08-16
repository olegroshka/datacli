"""Fetch event-level dividend history for qualifying US common stocks from EODHD.

Uses the dedicated `div/{ticker}.{exchange}` endpoint and the completed
US common-stock coverage summary under `data/raw/eodhd/us_common/`.

Usage:
    uv run python eodhd/fetch_eodhd_us_dividends.py
    uv run python eodhd/fetch_eodhd_us_dividends.py --tickers AAPL.US MSFT.US

Outputs:
    data/raw/eodhd/us_common/dividends_history.parquet
    data/raw/eodhd/us_common/dividends_fetch_audit.csv
    data/raw/eodhd/us_common/dividends_fetch_state.csv
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
from eodhd_event_fetch_common import (
    DELAY,
    EODHD_BASE,
    FLUSH_EVERY,
    HTTP_TIMEOUT,
    build_latest_date_state,
    build_pair_state_lookup,
    choose_incremental_window,
    compute_next_resume_from,
    load_completed_pairs,
    load_target_tickers,
    merge_audit_rows,
    merge_output_frame,
    merge_pair_state_rows,
    normalize_iso_date,
    rebuild_event_audit,
)
from fetch_eodhd_dividends import normalize_dividend_rows
from fetch_eodhd_us_fundamentals import _ROOT, _get_api_key

RAW_DIR = EODHD_RAW_ROOT / "us_common"
COVERAGE_PATH = RAW_DIR / "coverage_summary.csv"
DIVIDENDS_PATH = RAW_DIR / "dividends_history.parquet"
DIVIDENDS_AUDIT_PATH = RAW_DIR / "dividends_fetch_audit.csv"
DIVIDENDS_STATE_PATH = RAW_DIR / "dividends_fetch_state.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_dividends")


def _max_iso_date(*values: object) -> str | None:
    normalized = [
        value for value in (normalize_iso_date(v) for v in values) if value is not None
    ]
    return max(normalized) if normalized else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD US dividend history into the data root"
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
        default=None,
        help="Optional lower bound (YYYY-MM-DD). Defaults to full-history for new tickers.",
    )
    parser.add_argument(
        "--to",
        dest="to_date",
        default=datetime.now(timezone.utc).date().isoformat(),
        help="Upper bound (YYYY-MM-DD); defaults to current UTC date.",
    )
    parser.add_argument(
        "--overlap-days",
        type=int,
        default=5,
        help="Overlap days for incremental refreshes to absorb provider corrections.",
    )
    parser.add_argument(
        "--refresh-existing",
        action="store_true",
        help="Alias for --full-refresh; refetch targeted tickers from the requested lower bound.",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Ignore cached resume state and refetch targeted tickers from the requested lower bound.",
    )
    args = parser.parse_args()
    full_refresh = args.refresh_existing or args.full_refresh

    api_key = _get_api_key()
    targets = load_target_tickers(
        args.tickers, coverage_path=COVERAGE_PATH, limit=args.limit
    )
    log.info("US dividend target firms: %d", len(targets))

    _completed_pairs, existing_audit, existing_output = load_completed_pairs(
        audit_path=DIVIDENDS_AUDIT_PATH,
        output_path=DIVIDENDS_PATH,
    )
    existing_state = (
        pd.read_csv(DIVIDENDS_STATE_PATH) if DIVIDENDS_STATE_PATH.exists() else None
    )
    state_lookup = build_pair_state_lookup(existing_state)
    resume_state = build_latest_date_state(existing_output, date_column="ex_date")
    if state_lookup and not full_refresh:
        log.info(
            "Cached US dividend fetch-state rows on disk: %d firms", len(state_lookup)
        )

    session = requests.Session()
    session.params = {"api_token": api_key}

    new_frames: list[pd.DataFrame] = []
    new_audit_rows: list[dict[str, object]] = []
    new_state_rows: list[dict[str, object]] = []
    attempted = 0
    fetched = 0
    empty = 0
    skipped = 0

    def _flush() -> None:
        nonlocal existing_output, existing_audit, existing_state
        non_empty_frames = [
            frame for frame in new_frames if frame is not None and not frame.empty
        ]
        if non_empty_frames:
            new_df = pd.concat(non_empty_frames, ignore_index=True)
            merged = merge_output_frame(
                existing_output,
                new_df,
                key_columns=["ticker", "exchange", "ex_date", "dividend"],
            )
            DIVIDENDS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_parquet(merged, DIVIDENDS_PATH, index=False)
            existing_output = merged
        if new_audit_rows:
            existing_audit = merge_audit_rows(existing_audit, new_audit_rows)
        if new_state_rows:
            merged_state = merge_pair_state_rows(existing_state, new_state_rows)
            DIVIDENDS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(merged_state, DIVIDENDS_STATE_PATH, index=False)
            existing_state = merged_state
        rebuilt_audit = rebuild_event_audit(
            output=existing_output,
            state=existing_state,
            existing_audit=existing_audit,
        )
        if rebuilt_audit is not None and not rebuilt_audit.empty:
            DIVIDENDS_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(rebuilt_audit, DIVIDENDS_AUDIT_PATH, index=False)
            existing_audit = rebuilt_audit
        if non_empty_frames or new_audit_rows or new_state_rows:
            output_rows = (
                0
                if existing_output is None or existing_output.empty
                else len(existing_output)
            )
            output_firms = (
                0
                if existing_output is None or existing_output.empty
                else existing_output[["ticker", "exchange"]].drop_duplicates().shape[0]
            )
            log.info(
                "  [FLUSH] US dividends=%d rows (%d firms)", output_rows, output_firms
            )
        new_frames.clear()
        new_audit_rows.clear()
        new_state_rows.clear()

    for i, (ticker, exchange) in enumerate(targets):
        prior_state = state_lookup.get((ticker, exchange), {})
        prior_last_date = normalize_iso_date(
            resume_state.get((ticker, exchange), prior_state.get("latest_data_date"))
        )
        prior_coverage = normalize_iso_date(prior_state.get("coverage_through"))
        fetch_window = choose_incremental_window(
            last_date=prior_last_date,
            coverage_through=prior_coverage,
            from_date=args.from_date,
            to_date=args.to_date,
            overlap_days=args.overlap_days,
            full_refresh=full_refresh,
        )
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if fetch_window is None:
            skipped += 1
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "up_to_date",
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

        if fetched > 0 and fetched % 50 == 0:
            log.info(
                "Progress: %d/%d (fetched=%d, empty=%d, skipped=%d)",
                i + 1,
                len(targets),
                fetched,
                empty,
                skipped,
            )

        url = f"{EODHD_BASE}/div/{ticker}.{exchange}"
        params = {"fmt": "json", "to": fetch_to}
        if fetch_from:
            params["from"] = fetch_from
        try:
            response = session.get(url, params=params, timeout=HTTP_TIMEOUT)
        except requests.RequestException as exc:
            new_audit_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "request_error",
                    "n_rows": 0,
                    "detail": str(exc),
                    "fetched_at": fetched_at,
                }
            )
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "request_error",
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
                    "detail": str(exc),
                }
            )
            if attempted % FLUSH_EVERY == 0:
                _flush()
            time.sleep(DELAY)
            continue

        if response.status_code != 200:
            new_audit_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": f"http_{response.status_code}",
                    "n_rows": 0,
                    "detail": response.text[:200],
                    "fetched_at": fetched_at,
                }
            )
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": f"http_{response.status_code}",
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
            if attempted % FLUSH_EVERY == 0:
                _flush()
            time.sleep(DELAY)
            continue

        try:
            data = response.json()
        except Exception as exc:
            new_audit_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "decode_error",
                    "n_rows": 0,
                    "detail": str(exc),
                    "fetched_at": fetched_at,
                }
            )
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "decode_error",
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
                    "detail": str(exc),
                }
            )
            if attempted % FLUSH_EVERY == 0:
                _flush()
            time.sleep(DELAY)
            continue

        if not data or not isinstance(data, list):
            empty += 1
            new_audit_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "empty",
                    "n_rows": 0,
                    "detail": "",
                    "fetched_at": fetched_at,
                }
            )
            new_state_rows.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "status": "empty",
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
            if attempted % FLUSH_EVERY == 0:
                _flush()
            time.sleep(DELAY)
            continue

        df = normalize_dividend_rows(data, ticker, exchange)
        if not df.empty:
            new_frames.append(df)
        response_min_date = (
            normalize_iso_date(df["ex_date"].min()) if not df.empty else None
        )
        response_max_date = (
            normalize_iso_date(df["ex_date"].max()) if not df.empty else None
        )
        latest_data_date = _max_iso_date(prior_last_date, response_max_date)
        fetched += 1
        resume_state[(ticker, exchange)] = (
            latest_data_date or prior_last_date or fetch_to
        )
        new_audit_rows.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "status": "ok",
                "n_rows": len(df),
                "detail": "",
                "fetched_at": fetched_at,
            }
        )
        new_state_rows.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "status": "ok",
                "query_from": fetch_from,
                "query_to": fetch_to,
                "coverage_through": fetch_to,
                "previous_data_date": prior_last_date,
                "latest_data_date": latest_data_date,
                "response_rows": len(df),
                "response_min_date": response_min_date,
                "response_max_date": response_max_date,
                "overlap_days": args.overlap_days,
                "next_resume_from": compute_next_resume_from(
                    latest_data_date=latest_data_date,
                    coverage_through=fetch_to,
                    from_date=args.from_date,
                    overlap_days=args.overlap_days,
                ),
                "fetched_at": fetched_at,
                "detail": "",
            }
        )

        if attempted % FLUSH_EVERY == 0:
            _flush()

        time.sleep(DELAY)

    _flush()
    log.info("Done: fetched=%d, empty=%d, skipped=%d", fetched, empty, skipped)

    if DIVIDENDS_PATH.exists():
        final = pd.read_parquet(DIVIDENDS_PATH)
        print(
            f"\nUS dividends: {len(final):,} rows, "
            f"{final[['ticker', 'exchange']].drop_duplicates().shape[0]} firms"
        )
        print(f"Date range: {final['ex_date'].min()} -> {final['ex_date'].max()}")


if __name__ == "__main__":
    main()
