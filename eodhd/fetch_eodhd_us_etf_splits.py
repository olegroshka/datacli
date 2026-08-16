"""Fetch event-level split history for the US ETF starter sleeve from EODHD."""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timezone

import pandas as pd
import requests
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
    merge_audit_rows,
    merge_output_frame,
    merge_pair_state_rows,
    normalize_iso_date,
    rebuild_event_audit,
)
from fetch_eodhd_splits import normalize_split_rows
from fetch_eodhd_us_dividends import _max_iso_date
from fetch_eodhd_us_etf_universe import (
    ETF_TICKERS_PATH,
    RAW_DIR,
    STARTER_UNIVERSE_PATH,
    TARGET_UNIVERSES,
    load_target_tickers,
)
from fetch_eodhd_us_fundamentals import _get_api_key

SPLITS_PATH = RAW_DIR / "splits_history.parquet"
SPLITS_AUDIT_PATH = RAW_DIR / "splits_fetch_audit.csv"
SPLITS_STATE_PATH = RAW_DIR / "splits_fetch_state.csv"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_etf_splits")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD US ETF split history into the data root"
    )
    parser.add_argument(
        "--tickers", nargs="*", default=[], help="Explicit TICKER.EXCHANGE identifiers"
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Limit number of target ETFs"
    )
    parser.add_argument(
        "--universe",
        choices=TARGET_UNIVERSES,
        default="provider",
        help="Target the full provider ETF universe (default) or the curated starter sleeve",
    )
    parser.add_argument("--from", dest="from_date", default=None)
    parser.add_argument(
        "--to", dest="to_date", default=datetime.now(timezone.utc).date().isoformat()
    )
    parser.add_argument("--overlap-days", type=int, default=5)
    parser.add_argument("--full-refresh", action="store_true")
    args = parser.parse_args()

    api_key = _get_api_key()
    targets = load_target_tickers(
        explicit_specs=args.tickers,
        limit=args.limit,
        universe=args.universe,
        provider_path=ETF_TICKERS_PATH,
        starter_path=STARTER_UNIVERSE_PATH,
    )
    log.info("US ETF split target count (%s universe): %d", args.universe, len(targets))

    _, existing_audit, existing_output = load_completed_pairs(
        audit_path=SPLITS_AUDIT_PATH, output_path=SPLITS_PATH
    )
    existing_state = (
        pd.read_csv(SPLITS_STATE_PATH) if SPLITS_STATE_PATH.exists() else None
    )
    state_lookup = build_pair_state_lookup(existing_state)
    resume_state = build_latest_date_state(existing_output, date_column="ex_date")

    session = requests.Session()
    session.params = {"api_token": api_key}

    new_frames: list[pd.DataFrame] = []
    new_audit_rows: list[dict[str, object]] = []
    new_state_rows: list[dict[str, object]] = []
    attempted = fetched = empty = skipped = 0

    def _flush() -> None:
        nonlocal existing_output, existing_audit, existing_state
        frames = [
            frame for frame in new_frames if frame is not None and not frame.empty
        ]
        if frames:
            merged = merge_output_frame(
                existing_output,
                pd.concat(frames, ignore_index=True),
                key_columns=["ticker", "exchange", "ex_date", "split_factor"],
            )
            SPLITS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_parquet(merged, SPLITS_PATH, index=False)
            existing_output = merged
        if new_audit_rows:
            existing_audit = merge_audit_rows(existing_audit, new_audit_rows)
        if new_state_rows:
            merged_state = merge_pair_state_rows(existing_state, new_state_rows)
            SPLITS_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(merged_state, SPLITS_STATE_PATH, index=False)
            existing_state = merged_state
        rebuilt = rebuild_event_audit(
            output=existing_output, state=existing_state, existing_audit=existing_audit
        )
        if rebuilt is not None and not rebuilt.empty:
            SPLITS_AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
            _atomic.to_csv(rebuilt, SPLITS_AUDIT_PATH, index=False)
            existing_audit = rebuilt
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
            full_refresh=args.full_refresh,
        )
        fetched_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if fetch_window is None:
            skipped += 1
            continue
        fetch_from, fetch_to = fetch_window
        attempted += 1
        response = session.get(
            f"{EODHD_BASE}/splits/{ticker}.{exchange}",
            params={
                "fmt": "json",
                "to": fetch_to,
                **({"from": fetch_from} if fetch_from else {}),
            },
            timeout=HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            skipped += 1
            continue
        data = response.json()
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
        df = normalize_split_rows(data, ticker, exchange)
        if not df.empty:
            new_frames.append(df)
        response_min_date = (
            normalize_iso_date(df["ex_date"].min()) if not df.empty else None
        )
        response_max_date = (
            normalize_iso_date(df["ex_date"].max()) if not df.empty else None
        )
        latest_data_date = _max_iso_date(prior_last_date, response_max_date)
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
        fetched += 1
        if attempted % FLUSH_EVERY == 0:
            _flush()
        time.sleep(DELAY)

    _flush()
    log.info(
        "Done: attempted=%d, fetched=%d, empty=%d, skipped=%d",
        attempted,
        fetched,
        empty,
        skipped,
    )


if __name__ == "__main__":
    main()
