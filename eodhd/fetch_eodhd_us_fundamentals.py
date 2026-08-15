"""Fetch quarterly fundamentals for US-listed common stocks from EODHD.

This is the US common-stock counterpart to the completed UK/EU workflow.
The first slice targets the EODHD `US` exchange master list and filters it down
to `Type == "Common Stock"`, intentionally excluding ETFs, preferreds,
warrants, and other wrappers from this raw issuer universe.

Usage:
    # Smoke test (20 liquid US common stocks)
    uv run python eodhd/fetch_eodhd_us_fundamentals.py --smoke

    # Full pull (current-listed common stocks from the EODHD US master list)
    uv run python eodhd/fetch_eodhd_us_fundamentals.py

    # Explicit pull for named tickers
    uv run python eodhd/fetch_eodhd_us_fundamentals.py --tickers AAPL.US MSFT.US JPM.US

Outputs:
    data/raw/eodhd/us_common/tickers_US.parquet
    data/raw/eodhd/us_common/fundamentals_quarterly.parquet
    data/raw/eodhd/us_common/firm_metadata.parquet
    data/raw/eodhd/us_common/coverage_summary.csv
    data/raw/eodhd/us_common/cache/fundamentals/US/{ticker}.json.gz
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import fundamentals_refresh_common as fr
import pandas as pd
import requests
from _datadir import EODHD_RAW_ROOT
from fetch_eodhd_eu_fundamentals import (
    _ROOT,
    _api_get,
    _get_api_key,
    _merge_output_frame,
    _sanitize_path_component,
    compute_coverage,
    extract_general_info,
    extract_quarterly_statements,
    extract_same_call_section_frames,
    parse_ticker_spec,
)

RAW_DIR = EODHD_RAW_ROOT / "us_common"
RAW_CACHE_DIR = RAW_DIR / "cache" / "fundamentals"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_us_common_fetch")

TARGET_EXCHANGES = {
    "US": "United States master list (current-listed common stocks)",
}

ALLOWED_PRIMARY_EXCHANGES = {
    "NASDAQ",
    "NYSE",
    "NYSE MKT",
    "NYSE AMERICAN",
    "AMEX",
}

NON_COMMON_NAME_PATTERN = re.compile(
    r"\b(?:warrant|warrants|right|rights|unit|units|adr)\b|american depositary|depositary shares?",
    re.IGNORECASE,
)
NON_COMMON_CODE_PATTERN = re.compile(
    r"(?:-R|-U|-W)$|^[A-Z]{5,}(?:R|U|W)$", re.IGNORECASE
)

DELAY_BETWEEN_CALLS = 0.12

SMOKE_TICKERS = [
    ("AAPL", "US"),
    ("MSFT", "US"),
    ("AMZN", "US"),
    ("GOOGL", "US"),
    ("META", "US"),
    ("NVDA", "US"),
    ("JPM", "US"),
    ("BAC", "US"),
    ("BRK-B", "US"),
    ("XOM", "US"),
    ("CVX", "US"),
    ("JNJ", "US"),
    ("UNH", "US"),
    ("PG", "US"),
    ("KO", "US"),
    ("WMT", "US"),
    ("HD", "US"),
    ("COST", "US"),
    ("ABBV", "US"),
    ("MRK", "US"),
]


def _build_section_output_specs(
    raw_dir: Path = RAW_DIR,
) -> dict[str, dict[str, object]]:
    return {
        "splits_dividends_snapshot": {
            "path": raw_dir / "splits_dividends_snapshot.parquet",
            "keys": ["ticker", "exchange"],
        },
        "dividend_counts_by_year": {
            "path": raw_dir / "dividend_counts_by_year.parquet",
            "keys": ["ticker", "exchange", "year"],
        },
        "shares_stats_snapshot": {
            "path": raw_dir / "shares_stats_snapshot.parquet",
            "keys": ["ticker", "exchange"],
        },
        "highlights_snapshot": {
            "path": raw_dir / "highlights_snapshot.parquet",
            "keys": ["ticker", "exchange"],
        },
        "valuation_snapshot": {
            "path": raw_dir / "valuation_snapshot.parquet",
            "keys": ["ticker", "exchange"],
        },
        "outstanding_shares_annual": {
            "path": raw_dir / "outstanding_shares_annual.parquet",
            "keys": ["ticker", "exchange", "date"],
        },
        "outstanding_shares_quarterly": {
            "path": raw_dir / "outstanding_shares_quarterly.parquet",
            "keys": ["ticker", "exchange", "date"],
        },
        "earnings_history": {
            "path": raw_dir / "earnings_history.parquet",
            "keys": ["ticker", "exchange", "date"],
        },
        "earnings_trend": {
            "path": raw_dir / "earnings_trend.parquet",
            "keys": ["ticker", "exchange", "date", "period"],
        },
        "earnings_annual": {
            "path": raw_dir / "earnings_annual.parquet",
            "keys": ["ticker", "exchange", "date"],
        },
    }


SECTION_OUTPUT_SPECS = _build_section_output_specs()


def raw_cache_path(ticker: str, exchange: str, *, raw_dir: Path = RAW_DIR) -> Path:
    safe_exchange = _sanitize_path_component(exchange)
    safe_ticker = _sanitize_path_component(ticker)
    return raw_dir / "cache" / "fundamentals" / safe_exchange / f"{safe_ticker}.json.gz"


def load_cached_raw_payload(
    ticker: str, exchange: str, *, raw_dir: Path = RAW_DIR
) -> dict | None:
    cache_path = raw_cache_path(ticker, exchange, raw_dir=raw_dir)
    if not cache_path.exists():
        return None
    try:
        with gzip.open(cache_path, "rt", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        log.warning("Failed reading raw cache for %s.%s — %s", ticker, exchange, exc)
        return None
    return payload if isinstance(payload, dict) else None


def save_raw_payload(
    raw: dict, ticker: str, exchange: str, *, raw_dir: Path = RAW_DIR
) -> Path:
    cache_path = raw_cache_path(ticker, exchange, raw_dir=raw_dir)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(cache_path, "wt", encoding="utf-8") as handle:
        json.dump(raw, handle, ensure_ascii=False)
    return cache_path


def fetch_exchange_tickers(
    session: requests.Session, exchange: str = "US"
) -> pd.DataFrame:
    log.info(
        "Fetching ticker list for %s (%s)", exchange, TARGET_EXCHANGES.get(exchange, "")
    )
    data = _api_get(session, f"exchange-symbol-list/{exchange}")
    if not data or not isinstance(data, list):
        log.warning("No tickers for %s", exchange)
        return pd.DataFrame()
    df = pd.DataFrame(data)
    if "Type" in df.columns:
        df = df[df["Type"] == "Common Stock"].copy()
    if "Exchange" in df.columns and exchange == "US":
        exchange_series = df["Exchange"].astype(str).str.upper().str.strip()
        df = df[exchange_series.isin(ALLOWED_PRIMARY_EXCHANGES)].copy()
    if exchange == "US":
        name_series = df.get("Name", pd.Series(index=df.index, dtype="object")).astype(
            str
        )
        code_series = df.get("Code", pd.Series(index=df.index, dtype="object")).astype(
            str
        )
        wrapper_mask = name_series.str.contains(NON_COMMON_NAME_PATTERN, na=False)
        code_mask = code_series.str.contains(NON_COMMON_CODE_PATTERN, na=False)
        df = df[~(wrapper_mask | code_mask)].copy()
    log.info("  %s: %d common stocks", exchange, len(df))
    time.sleep(DELAY_BETWEEN_CALLS)
    return df


def fetch_fundamentals(
    session: requests.Session, ticker: str, exchange: str
) -> dict | None:
    endpoint = f"fundamentals/{ticker}.{exchange}"
    data = _api_get(session, endpoint)
    return data if isinstance(data, dict) else None


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD US common-stock fundamentals into the data root"
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="Smoke test: 20 known US common stocks only",
    )
    parser.add_argument(
        "--limit", type=int, default=0, help="Max firms per exchange (0=all)"
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Explicit EODHD identifiers in TICKER.EXCHANGE form; bypasses exchange scans",
    )
    parser.add_argument(
        "--refresh-raw",
        action="store_true",
        help="Ignore the private raw payload cache and refetch payloads from EODHD",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Refresh existing firms that reported since the last pull (calendar-targeted) "
        "plus any new firms; not just backfill missing firms",
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Refresh every firm (restatement sweep / hard rebuild)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=30,
        help="In --update without the calendar, re-fetch firms not pulled in this many days",
    )
    parser.add_argument(
        "--no-calendar",
        action="store_true",
        help="In --update, skip the earnings calendar and use --stale-days targeting",
    )
    parser.add_argument(
        "--reported-from",
        default="",
        help="Override the earnings-calendar window start (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--exchanges",
        nargs="+",
        default=list(TARGET_EXCHANGES.keys()),
        help="Exchange codes to fetch (default: US master list)",
    )
    args = parser.parse_args()

    api_key = _get_api_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    RAW_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.params = {"api_token": api_key}  # type: ignore[assignment]
    session.headers.update({"Accept": "application/json"})

    flush_every = 200

    fund_path = RAW_DIR / "fundamentals_quarterly.parquet"
    meta_path = RAW_DIR / "firm_metadata.parquet"

    existing_fund: pd.DataFrame | None = None
    existing_meta: pd.DataFrame | None = None
    cached_tickers: set[tuple[str, str]] = set()
    meta_tickers: set[tuple[str, str]] = set()
    existing_section_outputs: dict[str, pd.DataFrame | None] = {
        name: None for name in SECTION_OUTPUT_SPECS
    }
    section_tickers: dict[str, set[tuple[str, str]]] = {
        name: set() for name in SECTION_OUTPUT_SPECS
    }

    if fund_path.exists() and not args.smoke:
        existing_fund = pd.read_parquet(fund_path)
        cached_tickers = set(zip(existing_fund["ticker"], existing_fund["exchange"]))
        log.info(
            "Loaded %d cached firms from %s — will skip these",
            len(cached_tickers),
            fund_path.name,
        )
    if meta_path.exists() and not args.smoke:
        existing_meta = pd.read_parquet(meta_path)
        if {"ticker", "exchange_code"}.issubset(existing_meta.columns):
            meta_tickers = set(
                zip(existing_meta["ticker"], existing_meta["exchange_code"])
            )

    for output_name, spec in SECTION_OUTPUT_SPECS.items():
        output_path = Path(spec["path"])
        if output_path.exists() and not args.smoke:
            output_df = pd.read_parquet(output_path)
            existing_section_outputs[output_name] = output_df
            if {"ticker", "exchange"}.issubset(output_df.columns):
                section_tickers[output_name] = set(
                    zip(output_df["ticker"], output_df["exchange"])
                )

    new_fundamentals: list[pd.DataFrame] = []
    new_metadata: list[dict[str, object]] = []
    new_section_outputs: dict[str, list[pd.DataFrame]] = {
        name: [] for name in SECTION_OUTPUT_SPECS
    }
    total_materialized = 0
    total_api_payloads = 0
    total_raw_cache_hits = 0
    total_skipped = 0
    total_cached = 0

    if args.tickers:
        tickers_to_fetch = [parse_ticker_spec(value) for value in args.tickers]
        log.info("=" * 60)
        log.info("EXPLICIT US TICKER PULL -- %d tickers", len(tickers_to_fetch))
        log.info("=" * 60)
    elif args.smoke:
        log.info("=" * 60)
        log.info("US COMMON-STOCK SMOKE TEST -- 20 known tickers")
        log.info("=" * 60)
        tickers_to_fetch = SMOKE_TICKERS
    else:
        log.info("=" * 60)
        log.info("FULL US COMMON-STOCK PULL -- exchanges: %s", args.exchanges)
        log.info("=" * 60)
        tickers_to_fetch: list[tuple[str, str]] = []
        for exchange in args.exchanges:
            if exchange not in TARGET_EXCHANGES:
                log.warning("Unknown exchange: %s, skipping", exchange)
                continue
            ticker_path = RAW_DIR / f"tickers_{exchange}.parquet"
            if ticker_path.exists():
                ticker_df = pd.read_parquet(ticker_path)
                if "Type" in ticker_df.columns:
                    ticker_df = ticker_df[ticker_df["Type"] == "Common Stock"]
                log.info(
                    "  %s: loaded %d common stocks from cache", exchange, len(ticker_df)
                )
            else:
                ticker_df = fetch_exchange_tickers(session, exchange)
                if ticker_df.empty:
                    continue
                ticker_df.to_parquet(ticker_path, index=False)
                log.info(
                    "  %s: fetched and saved %d common stocks", exchange, len(ticker_df)
                )

            code_col = "Code" if "Code" in ticker_df.columns else ticker_df.columns[0]
            codes = ticker_df[code_col].astype(str).tolist()
            if args.limit > 0:
                codes = codes[: args.limit]
            tickers_to_fetch.extend((str(code), exchange) for code in codes)

    log.info(
        "Total tickers to process: %d (cached: %d)",
        len(tickers_to_fetch),
        sum(
            1
            for ticker, exchange in tickers_to_fetch
            if (ticker, exchange) in cached_tickers
        ),
    )

    def _flush_to_disk() -> None:
        nonlocal existing_fund, existing_meta, existing_section_outputs
        if (
            not new_fundamentals
            and not new_metadata
            and not any(new_section_outputs.values())
        ):
            return

        if new_fundamentals:
            new_df = pd.concat(new_fundamentals, ignore_index=True)
            merged = _merge_output_frame(
                existing_fund,
                new_df,
                key_columns=["ticker", "exchange", "statement", "date"],
            )
            merged.to_parquet(fund_path, index=False)
            existing_fund = merged

        if new_metadata:
            new_meta_df = pd.DataFrame(new_metadata)
            merged_meta = _merge_output_frame(
                existing_meta,
                new_meta_df,
                key_columns=["ticker", "exchange_code"],
            )
            merged_meta.to_parquet(meta_path, index=False)
            existing_meta = merged_meta

        for output_name, frames in new_section_outputs.items():
            non_empty_frames = [
                frame for frame in frames if frame is not None and not frame.empty
            ]
            if not non_empty_frames:
                continue
            new_output_df = pd.concat(non_empty_frames, ignore_index=True)
            merged_output = _merge_output_frame(
                existing_section_outputs.get(output_name),
                new_output_df,
                key_columns=list(SECTION_OUTPUT_SPECS[output_name]["keys"]),
            )
            output_path = Path(SECTION_OUTPUT_SPECS[output_name]["path"])
            merged_output.to_parquet(output_path, index=False)
            existing_section_outputs[output_name] = merged_output

        fund_rows = (
            0 if existing_fund is None or existing_fund.empty else len(existing_fund)
        )
        fund_firms = (
            0
            if existing_fund is None or existing_fund.empty
            else existing_fund[["ticker", "exchange"]].drop_duplicates().shape[0]
        )
        meta_firms = (
            0 if existing_meta is None or existing_meta.empty else len(existing_meta)
        )
        log.info(
            "  [FLUSH] fundamentals=%d rows (%d firms); metadata=%d firms",
            fund_rows,
            fund_firms,
            meta_firms,
        )
        new_fundamentals.clear()
        new_metadata.clear()
        for frames in new_section_outputs.values():
            frames.clear()

    now_ts = pd.Timestamp(datetime.now(timezone.utc)).tz_localize(None)
    if args.full_refresh:
        mode = fr.MODE_FULL
    elif args.update:
        mode = fr.MODE_UPDATE
    else:
        mode = fr.MODE_BACKFILL

    state_path = RAW_DIR / fr.STATE_FILENAME
    prior_state = fr.load_state(state_path)

    reported = None
    if mode == fr.MODE_UPDATE and not args.no_calendar:
        window_from = fr.resolve_reported_from(
            args.reported_from or None, existing_fund, as_of=now_ts
        )
        reported = fr.fetch_reported_symbols(
            session, window_from, now_ts.date().isoformat()
        )
        if reported is None:
            log.warning(
                "earnings calendar unavailable; falling back to --stale-days=%d",
                args.stale_days,
            )
        else:
            log.info(
                "earnings calendar: %d symbols reported since %s",
                len(reported),
                window_from,
            )

    targets = set(
        fr.select_targets(
            tickers_to_fetch,
            mode=mode,
            present=cached_tickers,
            state=prior_state,
            reported=reported,
            stale_days=args.stale_days,
            as_of=now_ts,
        )
    )
    force_refresh = mode in (fr.MODE_UPDATE, fr.MODE_FULL)
    refreshed_pairs: set[tuple[str, str]] = set()
    empty_pairs: set[tuple[str, str]] = set()
    log.info(
        "Refresh mode=%s -> %d firms targeted (of %d candidates)",
        mode,
        len(targets),
        len(tickers_to_fetch),
    )

    for i, (ticker, exchange) in enumerate(tickers_to_fetch):
        norm = fr.normalize_pair(ticker, exchange)
        if norm not in targets:
            total_cached += 1
            continue

        if force_refresh:
            needs_fundamentals = True
            needs_metadata = True
            missing_section_outputs = list(section_tickers)
        else:
            needs_fundamentals = (ticker, exchange) not in cached_tickers
            needs_metadata = (ticker, exchange) not in meta_tickers
            missing_section_outputs = [
                output_name
                for output_name, completed in section_tickers.items()
                if (ticker, exchange) not in completed
            ]

        if (
            not needs_fundamentals
            and not needs_metadata
            and not missing_section_outputs
        ):
            total_cached += 1
            continue

        if (total_materialized + total_skipped) % 50 == 0 and (
            total_materialized + total_skipped
        ) > 0:
            log.info(
                "Progress: %d / %d (materialized=%d, skipped=%d, raw_cache=%d, parquet_cached=%d)",
                i + 1,
                len(tickers_to_fetch),
                total_materialized,
                total_skipped,
                total_raw_cache_hits,
                total_cached,
            )

        use_cache = not (args.refresh_raw or force_refresh)
        raw = (
            load_cached_raw_payload(ticker, exchange, raw_dir=RAW_DIR)
            if use_cache
            else None
        )
        used_api = False
        if raw is not None:
            total_raw_cache_hits += 1
        else:
            raw = fetch_fundamentals(session, ticker, exchange)
            if raw is not None:
                save_raw_payload(raw, ticker, exchange, raw_dir=RAW_DIR)
                total_api_payloads += 1
                used_api = True

        if not raw:
            empty_pairs.add(norm)
            total_skipped += 1
            time.sleep(DELAY_BETWEEN_CALLS)
            continue

        materialized_this_ticker = False

        if needs_metadata:
            gen = extract_general_info(raw)
            gen["ticker"] = ticker
            gen["exchange_code"] = exchange
            new_metadata.append(gen)
            meta_tickers.add((ticker, exchange))
            materialized_this_ticker = True

        if needs_fundamentals:
            stmt_df = extract_quarterly_statements(raw, ticker, exchange)
            if not stmt_df.empty:
                new_fundamentals.append(stmt_df)
                cached_tickers.add((ticker, exchange))
                refreshed_pairs.add(norm)
                materialized_this_ticker = True
            else:
                empty_pairs.add(norm)
                total_skipped += 1

        if missing_section_outputs:
            section_frames = extract_same_call_section_frames(raw, ticker, exchange)
            for output_name in missing_section_outputs:
                frame = section_frames.get(output_name)
                if frame is None or frame.empty:
                    continue
                new_section_outputs[output_name].append(frame)
                section_tickers[output_name].add((ticker, exchange))
                materialized_this_ticker = True

        if materialized_this_ticker:
            total_materialized += 1

        if total_materialized > 0 and total_materialized % flush_every == 0:
            _flush_to_disk()

        if used_api:
            time.sleep(DELAY_BETWEEN_CALLS)

    _flush_to_disk()
    log.info(
        "Fetch complete: materialized=%d, api_payloads=%d, raw_cache=%d, skipped=%d, parquet_cached=%d",
        total_materialized,
        total_api_payloads,
        total_raw_cache_hits,
        total_skipped,
        total_cached,
    )

    if fund_path.exists():
        fundamentals_df = pd.read_parquet(fund_path)
    else:
        fundamentals_df = pd.DataFrame()
        log.warning("No fundamentals data on disk")

    fr.write_state(
        state_path,
        fundamentals_df,
        refreshed=refreshed_pairs,
        empty=empty_pairs,
        now=now_ts,
    )
    log.info(
        "Wrote %s (refreshed=%d, empty=%d)",
        state_path.name,
        len(refreshed_pairs),
        len(empty_pairs),
    )

    if meta_path.exists():
        meta_df = pd.read_parquet(meta_path)
        log.info("Metadata on disk: %d firms", len(meta_df))

    if not fundamentals_df.empty:
        coverage = compute_coverage(fundamentals_df)
        out_path = RAW_DIR / "coverage_summary.csv"
        coverage.to_csv(out_path, index=False)

        n_both_60q = coverage["both_60q"].sum()
        n_with_assets = (coverage["n_assets_2011_2025"] > 0).sum()
        n_with_capex = (coverage["n_capex_2011_2025"] > 0).sum()

        print("\n" + "=" * 70)
        print("EODHD US COMMON-STOCK FUNDAMENTALS — FETCH SUMMARY")
        print("=" * 70)
        print(f"  Firms materialized:               {total_materialized}")
        print(f"  Payloads fetched from API:        {total_api_payloads}")
        print(f"  Payloads reused from cache:       {total_raw_cache_hits}")
        print(f"  Firms already in parquet:         {total_cached}")
        print(f"  Firms skipped (no data):          {total_skipped}")
        print(f"  Firms with any Assets data:       {n_with_assets}")
        print(f"  Firms with any CapEx data:        {n_with_capex}")
        print(f"  Firms with both >=56Q (2011-2025): {n_both_60q}")
        print(f"  Output: {RAW_DIR}/")

        top = coverage.nlargest(20, "n_capex_2011_2025")
        print("\nTop 20 firms by CapEx coverage (2011-2025):")
        for _, row in top.iterrows():
            print(
                f"  {row['exchange']:<6} {row['ticker']:<10} assets={row['n_assets_2011_2025']:>3}Q  "
                f"capex={row['n_capex_2011_2025']:>3}Q  range={row['bs_first']}->{row['bs_last']}"
            )

        print("=" * 70 + "\n")
        log.info("Saved %s (%d rows)", out_path.name, len(coverage))


if __name__ == "__main__":
    main()
