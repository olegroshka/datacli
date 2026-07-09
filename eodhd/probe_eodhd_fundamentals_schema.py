"""Probe the structure of EODHD fundamentals payloads before broad downloads.

This helper is intentionally small and call-budget-aware:
- it uses a tiny pilot basket by default,
- it reuses the private raw payload cache when available,
- it saves newly fetched raw payloads for later offline parsing.

Usage:
    # Default: first 8 smoke-basket names
    EODHD_API_KEY=xxx uv run python eodhd/probe_eodhd_fundamentals_schema.py

    # Probe a smaller basket
    EODHD_API_KEY=xxx uv run python eodhd/probe_eodhd_fundamentals_schema.py --max-tickers 4

    # Probe explicit EODHD identifiers
    EODHD_API_KEY=xxx uv run python eodhd/probe_eodhd_fundamentals_schema.py --tickers SHEL.LSE SAP.XETRA

    # Force fresh payloads instead of using the private raw cache
    EODHD_API_KEY=xxx uv run python eodhd/probe_eodhd_fundamentals_schema.py --refresh-raw
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, TypedDict, cast

import requests

import fetch_eodhd_eu_fundamentals as fundamentals

DEFAULT_MAX_TICKERS = 8


class ProbeRow(TypedDict, total=False):
    ticker: str
    exchange: str
    source: str
    status: str
    top_level_sections: list[str]
    financial_subsections: list[str]
    section_summary: dict[str, dict[str, object]]


def _parse_ticker_spec(value: str) -> tuple[str, str]:
    if "." not in value:
        raise ValueError(f"Ticker spec must look like TICKER.EXCHANGE, got: {value}")
    ticker, exchange = value.rsplit(".", 1)
    ticker = ticker.strip()
    exchange = exchange.strip().upper()
    if not ticker or not exchange:
        raise ValueError(f"Ticker spec must look like TICKER.EXCHANGE, got: {value}")
    return ticker, exchange


def _build_probe_targets(args: argparse.Namespace) -> list[tuple[str, str]]:
    if args.tickers:
        return [_parse_ticker_spec(value) for value in args.tickers]
    return fundamentals.SMOKE_TICKERS[: args.max_tickers]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe EODHD fundamentals payload structure"
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Explicit EODHD identifiers in TICKER.EXCHANGE form",
    )
    parser.add_argument(
        "--max-tickers",
        type=int,
        default=DEFAULT_MAX_TICKERS,
        help="How many default smoke-basket names to probe (default: 8)",
    )
    parser.add_argument(
        "--refresh-raw",
        action="store_true",
        help="Ignore cached raw payloads and refetch the pilot payloads from EODHD",
    )
    parser.add_argument(
        "--out-json",
        type=str,
        default="",
        help="Optional path to write the probe report as JSON",
    )
    args = parser.parse_args()

    targets = _build_probe_targets(args)
    if not targets:
        raise RuntimeError("No probe targets selected")

    api_key = fundamentals._get_api_key()
    fundamentals.RAW_DIR.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    session.params = {"api_token": api_key}  # type: ignore[assignment]
    session.headers.update({"Accept": "application/json"})

    report: list[ProbeRow] = []
    union_sections: set[str] = set()
    api_count = 0
    cache_count = 0

    for ticker, exchange in targets:
        raw = (
            None
            if args.refresh_raw
            else fundamentals.load_cached_raw_payload(ticker, exchange)
        )
        source = "raw_cache"
        if raw is None:
            raw = fundamentals.fetch_fundamentals(session, ticker, exchange)
            source = "api"
            if raw is not None:
                fundamentals.save_raw_payload(raw, ticker, exchange)
                api_count += 1
        else:
            cache_count += 1

        if not raw:
            report.append(
                {
                    "ticker": ticker,
                    "exchange": exchange,
                    "source": source,
                    "status": "missing",
                    "top_level_sections": [],
                    "section_summary": {},
                }
            )
            continue

        section_summary = fundamentals.summarize_payload_sections(raw)
        top_level_sections = sorted(section_summary.keys())
        union_sections.update(top_level_sections)
        financial_subsections = []
        financials = raw.get("Financials", {})
        if isinstance(financials, dict):
            financial_subsections = sorted(financials.keys())

        report.append(
            {
                "ticker": ticker,
                "exchange": exchange,
                "source": source,
                "status": "ok",
                "top_level_sections": top_level_sections,
                "financial_subsections": financial_subsections,
                "section_summary": section_summary,
            }
        )

    print("\n" + "=" * 72)
    print("EODHD FUNDAMENTALS SCHEMA PROBE")
    print("=" * 72)
    print(f"Targets probed:              {len(targets)}")
    print(f"Payloads fetched from API:   {api_count}")
    print(f"Payloads reused from cache:  {cache_count}")
    print(
        f"Union of top-level sections: {', '.join(sorted(union_sections)) if union_sections else '(none)'}"
    )
    print("-" * 72)
    for row in report:
        ticker = cast(str, row["ticker"])
        exchange = cast(str, row["exchange"])
        source = cast(str, row["source"])
        status = cast(str, row["status"])
        print(f"{ticker}.{exchange:<8} source={source:<9} status={status}")
        if status == "ok":
            top_level_sections = cast(list[str], row.get("top_level_sections", []))
            print(f"  top-level:   {', '.join(top_level_sections)}")
            fin_sub = cast(list[str], row.get("financial_subsections", []))
            print(f"  financials:  {', '.join(fin_sub) if fin_sub else '(none)'}")
    print("=" * 72)

    if args.out_json:
        out_path = Path(args.out_json)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"JSON report written to: {out_path}")


if __name__ == "__main__":
    main()
