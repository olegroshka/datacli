"""Fetch daily EOD prices for qualifying UK/EU firms from EODHD.

Ported into `btest` from HARP with minimal semantic changes.
Source provenance:
    `harp/scripts/data_pipeline/fetch_eodhd_eu_prices.py`

Only fetches prices for firms that passed the >=56Q coverage threshold in the
fundamentals pull.

Supports incremental tail refreshes:

- new tickers get a full-history pull,
- existing tickers get only the missing tail from the last local date forward,
- overlap-based merging deduplicates on (`ticker`, `exchange`, `date`).

Usage:
    EODHD_API_KEY=xxx uv run python eodhd/fetch_eodhd_eu_prices.py

Outputs:
    data/raw/eodhd/uk_eu/prices_daily.parquet — daily OHLCV for qualifying firms
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_eu_fundamentals import _get_api_key, parse_ticker_spec

_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = EODHD_RAW_ROOT / "uk_eu"
COVERAGE_PATH = RAW_DIR / "coverage_summary.csv"
PRICES_PATH = RAW_DIR / "prices_daily.parquet"
PRICES_STATE_PATH = RAW_DIR / "prices_fetch_state.csv"

EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 60
DELAY = 0.12
FLUSH_EVERY = 100

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_prices")


DEFAULT_FROM = "2005-01-01"


def load_target_tickers(
    *,
    explicit_specs: list[str],
    limit: int,
) -> list[tuple[str, str]]:
    if explicit_specs:
        tickers = [parse_ticker_spec(value) for value in explicit_specs]
    else:
        if not COVERAGE_PATH.exists():
            raise RuntimeError(
                f"Run fetch_eodhd_eu_fundamentals.py first — {COVERAGE_PATH} not found"
            )
        cov = pd.read_csv(COVERAGE_PATH)
        qualifying = cov[cov["both_60q"] == 1][["ticker", "exchange"]].drop_duplicates()
        tickers = [tuple(row) for row in qualifying.itertuples(index=False, name=None)]
    if limit > 0:
        tickers = tickers[:limit]
    return tickers


def build_resume_state(existing: pd.DataFrame | None) -> dict[tuple[str, str], str]:
    if existing is None or existing.empty:
        return {}
    if not {"ticker", "exchange", "date"}.issubset(existing.columns):
        return {}
    latest = (
        existing.groupby(["ticker", "exchange"], as_index=False)["date"]
        .max()
        .itertuples(index=False, name=None)
    )
    return {
        (ticker, exchange): str(last_date) for ticker, exchange, last_date in latest
    }


def normalize_iso_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def build_fetch_state_lookup(
    existing: pd.DataFrame | None,
) -> dict[tuple[str, str], dict[str, object]]:
    if existing is None or existing.empty:
        return {}
    if not {"ticker", "exchange"}.issubset(existing.columns):
        return {}
    lookup: dict[tuple[str, str], dict[str, object]] = {}
    for row in existing.to_dict(orient="records"):
        ticker = str(row.get("ticker", "")).strip()
        exchange = str(row.get("exchange", "")).strip()
        if ticker and exchange:
            lookup[(ticker, exchange)] = row
    return lookup


def merge_fetch_state_rows(
    existing: pd.DataFrame | None, new_rows: list[dict[str, object]]
) -> pd.DataFrame:
    new_df = pd.DataFrame(new_rows)
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new_df], ignore_index=True)
    else:
        merged = new_df
    if {"ticker", "exchange"}.issubset(merged.columns):
        merged = merged.drop_duplicates(subset=["ticker", "exchange"], keep="last")
        merged = merged.sort_values(["exchange", "ticker"]).reset_index(drop=True)
    return merged


def compute_next_resume_from(
    *,
    latest_data_date: str | None,
    coverage_through: str | None,
    from_date: str,
    overlap_days: int,
) -> str:
    anchor = coverage_through if latest_data_date is None else latest_data_date
    if latest_data_date and coverage_through:
        latest_dt = datetime.fromisoformat(latest_data_date).date()
        coverage_dt = datetime.fromisoformat(coverage_through).date()
        anchor = coverage_through if latest_dt > coverage_dt else latest_data_date
    if anchor is None:
        return from_date
    anchor_dt = datetime.fromisoformat(anchor).date() - timedelta(
        days=max(overlap_days, 0)
    )
    floor_dt = datetime.fromisoformat(from_date).date()
    return max(anchor_dt, floor_dt).isoformat()


def choose_fetch_window(
    *,
    last_date: str | None,
    coverage_through: str | None = None,
    from_date: str,
    to_date: str,
    overlap_days: int,
    full_refresh: bool,
) -> tuple[str, str] | None:
    if full_refresh or (last_date is None and coverage_through is None):
        return from_date, to_date

    from_dt = datetime.fromisoformat(from_date).date()
    to_dt = datetime.fromisoformat(to_date).date()
    coverage_dt = (
        datetime.fromisoformat(str(coverage_through)).date()
        if coverage_through
        else None
    )
    last_dt = datetime.fromisoformat(str(last_date)).date() if last_date else None

    if coverage_dt is not None and coverage_dt >= to_dt:
        return None
    if coverage_dt is None and last_dt is not None and last_dt == to_dt:
        return None

    anchor_dt = coverage_dt if coverage_dt is not None else last_dt
    if last_dt is not None and (coverage_dt is None or last_dt <= coverage_dt):
        anchor_dt = last_dt
    if anchor_dt is None:
        return from_date, to_date
    anchor_dt = min(anchor_dt, to_dt)
    resume_from = max(from_dt, anchor_dt - timedelta(days=max(overlap_days, 0)))
    return resume_from.isoformat(), to_date


def _max_iso_date(*values: object) -> str | None:
    normalized = [
        value for value in (normalize_iso_date(v) for v in values) if value is not None
    ]
    return max(normalized) if normalized else None


def merge_price_frames(
    existing: pd.DataFrame | None, new: pd.DataFrame
) -> pd.DataFrame:
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new.copy()
    if {"ticker", "exchange", "date"}.issubset(merged.columns):
        merged = merged.drop_duplicates(
            subset=["ticker", "exchange", "date"], keep="last"
        )
        merged = merged.sort_values(["exchange", "ticker", "date"]).reset_index(
            drop=True
        )
    return merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD UK/EU daily prices into btest"
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
    log.info("Qualifying firms for price pull: %d", len(qualifying))

    existing: pd.DataFrame | None = None
    if PRICES_PATH.exists():
        existing = pd.read_parquet(PRICES_PATH)
        log.info(
            "Cached price rows on disk: %d (%d firms)",
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
                merged_state.to_csv(PRICES_STATE_PATH, index=False)
                existing_state = merged_state
                new_state_rows.clear()
            return
        new_df = pd.concat(new_frames, ignore_index=True)
        merged = merge_price_frames(existing, new_df)
        PRICES_PATH.parent.mkdir(parents=True, exist_ok=True)
        merged.to_parquet(PRICES_PATH, index=False)
        existing = merged
        if new_state_rows:
            merged_state = merge_fetch_state_rows(existing_state, new_state_rows)
            PRICES_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            merged_state.to_csv(PRICES_STATE_PATH, index=False)
            existing_state = merged_state
        log.info(
            "  [FLUSH] %d total price rows on disk (%d firms)",
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
            f"\nPrices: {len(final):,} rows, "
            f"{final[['ticker', 'exchange']].drop_duplicates().shape[0]} firms"
        )
        print(f"Date range: {final['date'].min()} -> {final['date'].max()}")


if __name__ == "__main__":
    main()
