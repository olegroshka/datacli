"""Fetch quarterly fundamentals for UK/EU-listed firms from EODHD.

Ported into `btest` from HARP with minimal semantic changes.
Source provenance:
    `harp/scripts/data_pipeline/fetch_eodhd_eu_fundamentals.py`

Pulls Balance Sheet, Cash Flow, and Income Statement quarterly data for
firms on target European exchanges. Stores raw data in parquet files for
later research use inside `btest`.

Usage:
    # Smoke test (20 firms)
    EODHD_API_KEY=xxx uv run python eodhd/fetch_eodhd_eu_fundamentals.py --smoke

    # Full pull (all common stocks on target exchanges)
    EODHD_API_KEY=xxx uv run python eodhd/fetch_eodhd_eu_fundamentals.py

    # Limit to N firms per exchange
    EODHD_API_KEY=xxx uv run python eodhd/fetch_eodhd_eu_fundamentals.py --limit 50

Outputs:
    data/raw/eodhd/uk_eu/tickers_{exchange}.parquet     — ticker lists per exchange
    data/raw/eodhd/uk_eu/fundamentals_quarterly.parquet — all quarterly BS + CF + IS data
    data/raw/eodhd/uk_eu/firm_metadata.parquet          — firm general info
    data/raw/eodhd/uk_eu/coverage_summary.csv           — per-firm coverage stats
    data/raw/eodhd/uk_eu/cache/fundamentals/{exchange}/{ticker}.json.gz — private raw payload cache
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import fundamentals_refresh_common as fr
import pandas as pd
import requests
import _atomic
from _datadir import EODHD_RAW_ROOT

#: Repo root (datacli). Key files and ``.env`` are looked up here first.
_ROOT = Path(__file__).resolve().parents[1]
#: The repo's parent -- where the original ``btest`` layout kept
#: ``configs/local``; still searched so existing setups keep working.
_LEGACY_ROOT = _ROOT.parent
RAW_DIR = EODHD_RAW_ROOT / "uk_eu"
RAW_CACHE_DIR = RAW_DIR / "cache" / "fundamentals"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("eodhd_fetch")

# ── Config ────────────────────────────────────────────────────────────────────

TARGET_EXCHANGES = {
    "LSE": "London Stock Exchange",
    "XETRA": "Frankfurt / Xetra",
    "PA": "Euronext Paris",
    "AS": "Euronext Amsterdam",
    "SW": "SIX Swiss Exchange",
    "MI": "Borsa Italiana",
    "MC": "Bolsa de Madrid",
    "ST": "Stockholm Exchange",
    "OL": "Oslo Stock Exchange",
    "HE": "Helsinki Exchange",
    "CO": "Copenhagen Exchange",
    "VI": "Vienna Exchange",
}

EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 60
DELAY_BETWEEN_CALLS = 0.12  # ~8 req/s, well within limits

SECTION_OUTPUT_SPECS = {
    "splits_dividends_snapshot": {
        "path": RAW_DIR / "splits_dividends_snapshot.parquet",
        "keys": ["ticker", "exchange"],
    },
    "dividend_counts_by_year": {
        "path": RAW_DIR / "dividend_counts_by_year.parquet",
        "keys": ["ticker", "exchange", "year"],
    },
    "shares_stats_snapshot": {
        "path": RAW_DIR / "shares_stats_snapshot.parquet",
        "keys": ["ticker", "exchange"],
    },
    "highlights_snapshot": {
        "path": RAW_DIR / "highlights_snapshot.parquet",
        "keys": ["ticker", "exchange"],
    },
    "valuation_snapshot": {
        "path": RAW_DIR / "valuation_snapshot.parquet",
        "keys": ["ticker", "exchange"],
    },
    "outstanding_shares_annual": {
        "path": RAW_DIR / "outstanding_shares_annual.parquet",
        "keys": ["ticker", "exchange", "date"],
    },
    "outstanding_shares_quarterly": {
        "path": RAW_DIR / "outstanding_shares_quarterly.parquet",
        "keys": ["ticker", "exchange", "date"],
    },
    "earnings_history": {
        "path": RAW_DIR / "earnings_history.parquet",
        "keys": ["ticker", "exchange", "date"],
    },
    "earnings_trend": {
        "path": RAW_DIR / "earnings_trend.parquet",
        "keys": ["ticker", "exchange", "date", "period"],
    },
    "earnings_annual": {
        "path": RAW_DIR / "earnings_annual.parquet",
        "keys": ["ticker", "exchange", "date"],
    },
}

# Fields to extract from quarterly statements
BS_FIELDS = [
    "totalAssets",
    "totalCurrentAssets",
    "totalNonCurrentAssets",
    "totalLiab",
    "totalStockholderEquity",
    "propertyPlantEquipment",
    "intangibleAssets",
    "goodWill",
    "longTermDebt",
    "shortTermDebt",
    "cash",
    "netReceivables",
    "inventory",
]

CF_FIELDS = [
    "capitalExpenditures",
    "totalCashFromOperatingActivities",
    "totalCashflowsFromInvestingActivities",
    "totalCashFromFinancingActivities",
    "freeCashFlow",
    "netIncome",
    "depreciation",
    "changeInWorkingCapital",
]

IS_FIELDS = [
    "totalRevenue",
    "grossProfit",
    "operatingIncome",
    "ebit",
    "ebitda",
    "netIncome",
    "researchDevelopment",
]

# Smoke test tickers (verified to have data)
SMOKE_TICKERS = [
    ("SHEL", "LSE"),
    ("BP", "LSE"),
    ("AZN", "LSE"),
    ("HSBA", "LSE"),
    ("GSK", "LSE"),
    ("ULVR", "LSE"),
    ("DGE", "LSE"),
    ("BARC", "LSE"),
    ("SAP", "XETRA"),
    ("SIE", "XETRA"),
    ("BAS", "XETRA"),
    ("BAYN", "XETRA"),
    ("BMW", "XETRA"),
    ("ALV", "XETRA"),
    ("TTE", "PA"),
    ("BNP", "PA"),
    ("MC", "PA"),
    ("SAN", "PA"),
    ("NESN", "SW"),
    ("NOVN", "SW"),
]


# ── API helpers ───────────────────────────────────────────────────────────────


def _get_api_key_from_windows_user_env() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as env_key:
            value, _ = winreg.QueryValueEx(env_key, "EODHD_API_KEY")
            return str(value).strip()
    except Exception:
        return ""


#: Where the EODHD key is looked for, in order (documented in the READMEs).
API_KEY_SOURCES = (
    "EODHD_API_KEY environment variable",
    "EODHD_API_KEY in the Windows user environment (setx)",
    "<repo>/configs/local/eodhd_api_key.txt or <repo>/local_cache/eodhd_api_key.txt",
    "EODHD_API_KEY=... in ./.env or <repo>/.env",
)


def _get_api_key() -> str:
    """Resolve the EODHD API key (see :data:`API_KEY_SOURCES`).

    Raises:
        RuntimeError: when no source yields a key.
    """
    key = os.environ.get("EODHD_API_KEY", "").strip()
    if key:
        return key

    key = _get_api_key_from_windows_user_env()
    if key:
        os.environ["EODHD_API_KEY"] = key
        return key

    key_locations = [
        _ROOT / "configs" / "local" / "eodhd_api_key.txt",
        _ROOT / "local_cache" / "eodhd_api_key.txt",
        _LEGACY_ROOT / "configs" / "local" / "eodhd_api_key.txt",
        _LEGACY_ROOT / "local_cache" / "eodhd_api_key.txt",
    ]
    for key_file in key_locations:
        try:
            if key_file.exists():
                key = key_file.read_text(encoding="utf-8").strip()
                if key:
                    os.environ["EODHD_API_KEY"] = key
                    return key
        except Exception:
            continue

    for env_file in [Path.cwd() / ".env", _ROOT / ".env", _LEGACY_ROOT / ".env"]:
        try:
            if not env_file.exists():
                continue
            for line in env_file.read_text(encoding="utf-8").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if stripped.startswith("EODHD_API_KEY="):
                    key = stripped.split("=", 1)[1].strip().strip('"').strip("'")
                    if key:
                        os.environ["EODHD_API_KEY"] = key
                        return key
        except Exception:
            continue

    raise RuntimeError(
        "EODHD API key not found. Looked in: "
        + "; ".join(API_KEY_SOURCES)
        + f" (repo = {_ROOT}). Easiest: $env:EODHD_API_KEY = '<key>' "
        "(persist with setx EODHD_API_KEY <key>). Get a key at "
        "https://eodhd.com/register"
    )


def _sanitize_path_component(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned or "_"


def parse_ticker_spec(value: str) -> tuple[str, str]:
    if "." not in value:
        raise ValueError(f"Ticker spec must look like TICKER.EXCHANGE, got: {value}")
    ticker, exchange = value.rsplit(".", 1)
    ticker = ticker.strip()
    exchange = exchange.strip().upper()
    if not ticker or not exchange:
        raise ValueError(f"Ticker spec must look like TICKER.EXCHANGE, got: {value}")
    return ticker, exchange


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


def summarize_payload_sections(
    raw: dict, *, max_keys: int = 12
) -> dict[str, dict[str, object]]:
    summary: dict[str, dict[str, object]] = {}
    for section_name, value in raw.items():
        section_summary: dict[str, object] = {"type": type(value).__name__}
        if isinstance(value, dict):
            section_summary["n_keys"] = len(value)
            section_summary["sample_keys"] = sorted(value.keys())[:max_keys]
        elif isinstance(value, list):
            section_summary["n_items"] = len(value)
            if value and isinstance(value[0], dict):
                section_summary["sample_keys"] = sorted(value[0].keys())[:max_keys]
        summary[section_name] = section_summary
    return summary


def _api_get(
    session: requests.Session,
    endpoint: str,
    params: dict | None = None,
) -> dict | list | None:
    url = f"{EODHD_BASE}/{endpoint}"
    all_params = {"fmt": "json"}
    if params:
        all_params.update(params)
    try:
        response = session.get(url, params=all_params, timeout=HTTP_TIMEOUT)
    except requests.RequestException as exc:
        log.warning("Request failed: %s — %s", endpoint, exc)
        return None
    if response.status_code == 429:
        log.warning("Rate limited on %s, sleeping 5s", endpoint)
        time.sleep(5)
        try:
            response = session.get(url, params=all_params, timeout=HTTP_TIMEOUT)
        except requests.RequestException:
            return None
    if response.status_code != 200:
        log.debug("HTTP %s for %s", response.status_code, endpoint)
        return None
    try:
        return response.json()
    except Exception:
        return None


# ── Step 1: Fetch exchange ticker lists ───────────────────────────────────────


def fetch_exchange_tickers(session: requests.Session, exchange: str) -> pd.DataFrame:
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
    log.info("  %s: %d common stocks", exchange, len(df))
    time.sleep(DELAY_BETWEEN_CALLS)
    return df


# ── Step 2: Fetch fundamentals for one firm ───────────────────────────────────


def fetch_fundamentals(
    session: requests.Session, ticker: str, exchange: str
) -> dict | None:
    endpoint = f"fundamentals/{ticker}.{exchange}"
    data = _api_get(session, endpoint)
    return data if isinstance(data, dict) else None


def extract_general_info(raw: dict) -> dict:
    gen = raw.get("General", {}) or {}
    return {
        "name": gen.get("Name", ""),
        "country": gen.get("CountryISO", gen.get("Country", "")),
        "currency": gen.get("CurrencyCode", gen.get("CurrencySymbol", "")),
        "exchange": gen.get("Exchange", ""),
        "sector": gen.get("GicsSector", gen.get("Sector", "")),
        "industry": gen.get("GicsIndustry", gen.get("Industry", "")),
        "isin": gen.get("ISIN", ""),
        "market_cap": gen.get("MarketCapitalization", None),
    }


def extract_quarterly_statements(raw: dict, ticker: str, exchange: str) -> pd.DataFrame:
    fin = raw.get("Financials", {}) or {}
    rows: list[dict] = []

    for stmt_name, stmt_key, fields in [
        ("BS", "Balance_Sheet", BS_FIELDS),
        ("CF", "Cash_Flow", CF_FIELDS),
        ("IS", "Income_Statement", IS_FIELDS),
    ]:
        stmt = fin.get(stmt_key, {}) or {}
        quarterly = stmt.get("quarterly", {}) or {}
        currency = stmt.get("currency_symbol", "")
        for date_key, values in quarterly.items():
            if not isinstance(values, dict):
                continue
            row = {
                "ticker": ticker,
                "exchange": exchange,
                "statement": stmt_name,
                "date": date_key,
                "filing_date": values.get("filing_date", ""),
                "currency": values.get("currency_symbol", currency),
            }
            for field in fields:
                val = values.get(field)
                if val is not None and val != "None" and val != "":
                    numeric_val = pd.to_numeric(val, errors="coerce")
                    if pd.notna(numeric_val):
                        row[field] = float(numeric_val)
                    else:
                        row[field] = None
                else:
                    row[field] = None
            rows.append(row)

    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


def _snake_case(value: str) -> str:
    step1 = re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", value)
    step2 = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step1)
    return step2.replace("-", "_").replace(" ", "_").lower()


def _normalize_scalar(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if stripped == "" or stripped == "None":
            return None
        numeric_val = pd.to_numeric(stripped, errors="coerce")
        if pd.notna(numeric_val):
            return float(numeric_val)
        return stripped
    if isinstance(value, (int, float)):
        return None if pd.isna(value) else value
    return value


def _normalize_record(record: dict[str, object]) -> dict[str, object]:
    return {_snake_case(key): _normalize_scalar(value) for key, value in record.items()}


def _single_row_snapshot(
    section: dict[str, object],
    *,
    ticker: str,
    exchange: str,
    exclude_keys: set[str] | None = None,
) -> pd.DataFrame:
    exclude_keys = exclude_keys or set()
    row: dict[str, object] = {"ticker": ticker, "exchange": exchange}
    for key, value in section.items():
        if key in exclude_keys or isinstance(value, (dict, list)):
            continue
        row[_snake_case(key)] = _normalize_scalar(value)
    if len(row) <= 2:
        return pd.DataFrame()
    return pd.DataFrame([row])


def _records_from_mapping(
    mapping: dict[str, object],
    *,
    ticker: str,
    exchange: str,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for _, record in mapping.items():
        if not isinstance(record, dict):
            continue
        row = {"ticker": ticker, "exchange": exchange}
        row.update(_normalize_record(record))
        rows.append(row)
    return rows


def _merge_output_frame(
    existing: pd.DataFrame | None,
    new: pd.DataFrame,
    *,
    key_columns: list[str],
) -> pd.DataFrame:
    if existing is not None and not existing.empty:
        merged = pd.concat([existing, new], ignore_index=True)
    else:
        merged = new.copy()
    if all(column in merged.columns for column in key_columns):
        merged = merged.drop_duplicates(subset=key_columns, keep="last")
        merged = merged.sort_values(key_columns).reset_index(drop=True)
    return merged


def extract_same_call_section_frames(
    raw: dict, ticker: str, exchange: str
) -> dict[str, pd.DataFrame]:
    frames: dict[str, pd.DataFrame] = {
        name: pd.DataFrame() for name in SECTION_OUTPUT_SPECS
    }

    splits_dividends = raw.get("SplitsDividends", {}) or {}
    if isinstance(splits_dividends, dict):
        frames["splits_dividends_snapshot"] = _single_row_snapshot(
            splits_dividends,
            ticker=ticker,
            exchange=exchange,
            exclude_keys={"NumberDividendsByYear"},
        )
        counts = splits_dividends.get("NumberDividendsByYear", {}) or {}
        if isinstance(counts, dict):
            rows = []
            for row in _records_from_mapping(counts, ticker=ticker, exchange=exchange):
                rows.append(
                    {
                        "ticker": ticker,
                        "exchange": exchange,
                        "year": (
                            int(row.get("year"))
                            if row.get("year") is not None
                            else None
                        ),
                        "count": (
                            int(row.get("count"))
                            if row.get("count") is not None
                            else None
                        ),
                    }
                )
            if rows:
                frames["dividend_counts_by_year"] = pd.DataFrame(rows)

    shares_stats = raw.get("SharesStats", {}) or {}
    if isinstance(shares_stats, dict):
        frames["shares_stats_snapshot"] = _single_row_snapshot(
            shares_stats,
            ticker=ticker,
            exchange=exchange,
        )

    highlights = raw.get("Highlights", {}) or {}
    if isinstance(highlights, dict):
        frames["highlights_snapshot"] = _single_row_snapshot(
            highlights,
            ticker=ticker,
            exchange=exchange,
        )

    valuation = raw.get("Valuation", {}) or {}
    if isinstance(valuation, dict):
        frames["valuation_snapshot"] = _single_row_snapshot(
            valuation,
            ticker=ticker,
            exchange=exchange,
        )

    outstanding_shares = raw.get("outstandingShares", {}) or {}
    if isinstance(outstanding_shares, dict):
        annual = outstanding_shares.get("annual", {}) or {}
        quarterly = outstanding_shares.get("quarterly", {}) or {}
        if isinstance(annual, dict):
            rows = _records_from_mapping(annual, ticker=ticker, exchange=exchange)
            if rows:
                frames["outstanding_shares_annual"] = pd.DataFrame(rows)
        if isinstance(quarterly, dict):
            rows = _records_from_mapping(quarterly, ticker=ticker, exchange=exchange)
            if rows:
                frames["outstanding_shares_quarterly"] = pd.DataFrame(rows)

    earnings = raw.get("Earnings", {}) or {}
    if isinstance(earnings, dict):
        for source_key, frame_name in [
            ("History", "earnings_history"),
            ("Trend", "earnings_trend"),
            ("Annual", "earnings_annual"),
        ]:
            section = earnings.get(source_key, {}) or {}
            if isinstance(section, dict):
                rows = _records_from_mapping(section, ticker=ticker, exchange=exchange)
                if rows:
                    frames[frame_name] = pd.DataFrame(rows)

    return frames


# ── Step 3: Coverage analysis ─────────────────────────────────────────────────


def compute_coverage(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    def _summarize(sub: pd.DataFrame, prefix: str) -> dict:
        if sub.empty:
            return {f"{prefix}_n": 0, f"{prefix}_first": "", f"{prefix}_last": ""}
        dates = sorted(sub["date"].dropna().unique())
        return {
            f"{prefix}_n": len(dates),
            f"{prefix}_first": dates[0] if dates else "",
            f"{prefix}_last": dates[-1] if dates else "",
        }

    records = []
    for (ticker, exchange), group in df.groupby(["ticker", "exchange"]):
        bs_sub = group[group["statement"] == "BS"]
        cf_sub = group[group["statement"] == "CF"]
        has_assets = (
            bs_sub["totalAssets"].notna().sum()
            if "totalAssets" in bs_sub.columns
            else 0
        )
        has_capex = (
            cf_sub["capitalExpenditures"].notna().sum()
            if "capitalExpenditures" in cf_sub.columns
            else 0
        )
        rec = {
            "ticker": ticker,
            "exchange": exchange,
            **_summarize(bs_sub, "bs"),
            **_summarize(cf_sub, "cf"),
            "n_assets": int(has_assets),
            "n_capex": int(has_capex),
        }
        bs_in_window = bs_sub[
            (bs_sub["date"] >= "2011-01-01") & (bs_sub["date"] <= "2025-12-31")
        ]
        cf_in_window = cf_sub[
            (cf_sub["date"] >= "2011-01-01") & (cf_sub["date"] <= "2025-12-31")
        ]
        assets_in_window = (
            bs_in_window["totalAssets"].notna().sum()
            if "totalAssets" in bs_in_window.columns
            else 0
        )
        capex_in_window = (
            cf_in_window["capitalExpenditures"].notna().sum()
            if "capitalExpenditures" in cf_in_window.columns
            else 0
        )
        rec["n_assets_2011_2025"] = int(assets_in_window)
        rec["n_capex_2011_2025"] = int(capex_in_window)
        rec["both_60q"] = int(min(assets_in_window, capex_in_window) >= 56)
        records.append(rec)

    return pd.DataFrame(records).sort_values(["exchange", "ticker"])


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch EODHD UK/EU fundamentals into the data root"
    )
    parser.add_argument(
        "--smoke", action="store_true", help="Smoke test: 20 known tickers only"
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
        help="Exchange codes to fetch",
    )
    args = parser.parse_args()

    api_key = _get_api_key()
    RAW_DIR.mkdir(parents=True, exist_ok=True)

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
        output_path = spec["path"]
        if output_path.exists() and not args.smoke:
            output_df = pd.read_parquet(output_path)
            existing_section_outputs[output_name] = output_df
            if {"ticker", "exchange"}.issubset(output_df.columns):
                section_tickers[output_name] = set(
                    zip(output_df["ticker"], output_df["exchange"])
                )

    new_fundamentals: list[pd.DataFrame] = []
    new_metadata: list[dict] = []
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
        log.info("EXPLICIT TICKER PULL -- %d tickers", len(tickers_to_fetch))
        log.info("=" * 60)
    elif args.smoke:
        log.info("=" * 60)
        log.info("SMOKE TEST -- 20 known tickers")
        log.info("=" * 60)
        tickers_to_fetch = SMOKE_TICKERS
    else:
        log.info("=" * 60)
        log.info("FULL PULL -- exchanges: %s", args.exchanges)
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
                _atomic.to_parquet(ticker_df, ticker_path, index=False)
                log.info(
                    "  %s: fetched and saved %d common stocks", exchange, len(ticker_df)
                )

            code_col = "Code" if "Code" in ticker_df.columns else ticker_df.columns[0]
            codes = ticker_df[code_col].astype(str).tolist()
            if args.limit > 0:
                codes = codes[: args.limit]
            for code in codes:
                tickers_to_fetch.append((str(code), exchange))

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
            _atomic.to_parquet(merged, fund_path, index=False)
            existing_fund = merged

        if new_metadata:
            new_meta_df = pd.DataFrame(new_metadata)
            merged_meta = _merge_output_frame(
                existing_meta,
                new_meta_df,
                key_columns=["ticker", "exchange_code"],
            )
            _atomic.to_parquet(merged_meta, meta_path, index=False)
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
            output_path = SECTION_OUTPUT_SPECS[output_name]["path"]
            _atomic.to_parquet(merged_output, output_path, index=False)
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
        raw = load_cached_raw_payload(ticker, exchange) if use_cache else None
        used_api = False
        if raw is not None:
            total_raw_cache_hits += 1
        else:
            raw = fetch_fundamentals(session, ticker, exchange)
            if raw is not None:
                save_raw_payload(raw, ticker, exchange)
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
        _atomic.to_csv(coverage, out_path, index=False)
        n_both_60q = coverage["both_60q"].sum()
        n_with_assets = (coverage["n_assets_2011_2025"] > 0).sum()
        n_with_capex = (coverage["n_capex_2011_2025"] > 0).sum()

        print("\n" + "=" * 70)
        print("EODHD UK/EU FUNDAMENTALS — FETCH SUMMARY")
        print("=" * 70)
        print(f"  Firms materialized:          {total_materialized}")
        print(f"  Payloads fetched from API:   {total_api_payloads}")
        print(f"  Payloads reused from cache:  {total_raw_cache_hits}")
        print(f"  Firms already in parquet:    {total_cached}")
        print(f"  Firms skipped (no data):      {total_skipped}")
        print(f"  Firms with any Assets data:   {n_with_assets}")
        print(f"  Firms with any CapEx data:    {n_with_capex}")
        print(f"  Firms with both >=56Q (2011-2025): {n_both_60q}")
        print(f"  Output: {RAW_DIR}/")
        if n_both_60q >= 80:
            print(f"\n  >> GATE G1 THRESHOLD MET: {n_both_60q} >= 80 firms")
        elif n_both_60q >= 60:
            print(f"\n  >> PIVOT A: {n_both_60q} firms (60-79 range)")
        else:
            print(f"\n  >> BELOW THRESHOLD: {n_both_60q} firms")

        top = coverage.nlargest(20, "n_capex_2011_2025")
        print("\nTop 20 firms by CapEx coverage (2011-2025):")
        for _, row in top.iterrows():
            print(
                f"  {row['exchange']:<6} {row['ticker']:<8} assets={row['n_assets_2011_2025']:>3}Q  "
                f"capex={row['n_capex_2011_2025']:>3}Q  range={row['bs_first']}->{row['bs_last']}"
            )

        print("\nCoverage by exchange:")
        for exchange in sorted(coverage["exchange"].unique()):
            sub = coverage[coverage["exchange"] == exchange]
            n60 = sub["both_60q"].sum()
            print(
                f"  {exchange:<6}: {len(sub):>4} firms total, {n60:>3} with >=56Q both"
            )

        print("=" * 70 + "\n")
        log.info("Saved %s (%d rows)", out_path.name, len(coverage))


if __name__ == "__main__":
    main()
