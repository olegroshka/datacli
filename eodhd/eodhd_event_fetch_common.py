"""Shared helpers for dedicated EODHD event-history fetchers.

These helpers keep the dividend and split endpoint fetchers consistent with the
cache-aware UK/EU workflow already established in `btest`.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

import pandas as pd

from fetch_eodhd_eu_fundamentals import parse_ticker_spec
from _datadir import EODHD_RAW_ROOT

_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = EODHD_RAW_ROOT / "uk_eu"
COVERAGE_PATH = RAW_DIR / "coverage_summary.csv"
EODHD_BASE = "https://eodhd.com/api"
HTTP_TIMEOUT = 60
DELAY = 0.12
FLUSH_EVERY = 100
SUCCESS_AUDIT_STATUSES = {"ok", "empty"}


def load_target_tickers(
    explicit_specs: Iterable[str],
    *,
    coverage_path: Path = COVERAGE_PATH,
    limit: int = 0,
) -> list[tuple[str, str]]:
    explicit_specs = list(explicit_specs)
    if explicit_specs:
        tickers = [parse_ticker_spec(value) for value in explicit_specs]
    else:
        if not coverage_path.exists():
            raise RuntimeError(f"Coverage file not found: {coverage_path}")
        cov = pd.read_csv(coverage_path)
        qualifying = cov[cov["both_60q"] == 1][["ticker", "exchange"]].drop_duplicates()
        tickers = [tuple(row) for row in qualifying.itertuples(index=False, name=None)]
    if limit > 0:
        tickers = tickers[:limit]
    return tickers


def normalize_scalar(value: object) -> object:
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


def normalize_iso_date(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def build_latest_date_state(
    existing: pd.DataFrame | None,
    *,
    date_column: str,
) -> dict[tuple[str, str], str]:
    if existing is None or existing.empty:
        return {}
    if not {"ticker", "exchange", date_column}.issubset(existing.columns):
        return {}
    latest = (
        existing.groupby(["ticker", "exchange"], as_index=False)[date_column]
        .max()
        .itertuples(index=False, name=None)
    )
    return {
        (ticker, exchange): str(last_date)
        for ticker, exchange, last_date in latest
        if normalize_iso_date(last_date)
    }


def build_pair_state_lookup(
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


def choose_incremental_window(
    *,
    last_date: str | None,
    coverage_through: str | None,
    from_date: str | None,
    to_date: str,
    overlap_days: int,
    full_refresh: bool,
) -> tuple[str | None, str] | None:
    if full_refresh or (last_date is None and coverage_through is None):
        return from_date, to_date

    to_dt = datetime.fromisoformat(to_date).date()
    coverage_dt = (
        datetime.fromisoformat(coverage_through).date() if coverage_through else None
    )
    last_dt = datetime.fromisoformat(last_date).date() if last_date else None

    if coverage_dt is not None and coverage_dt >= to_dt:
        return None
    if coverage_dt is None and last_dt is not None and to_dt == last_dt:
        return None

    anchor_dt = coverage_dt if coverage_dt is not None else last_dt
    if last_dt is not None and (coverage_dt is None or last_dt <= coverage_dt):
        anchor_dt = last_dt
    if anchor_dt is None:
        return from_date, to_date

    anchor_dt = min(anchor_dt, to_dt)
    resume_from_dt = anchor_dt - timedelta(days=max(overlap_days, 0))
    if from_date:
        resume_from_dt = max(resume_from_dt, datetime.fromisoformat(from_date).date())
    return resume_from_dt.isoformat(), to_date


def compute_next_resume_from(
    *,
    latest_data_date: str | None,
    coverage_through: str | None,
    from_date: str | None,
    overlap_days: int,
) -> str | None:
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
    if from_date:
        anchor_dt = max(anchor_dt, datetime.fromisoformat(from_date).date())
    return anchor_dt.isoformat()


def merge_pair_state_rows(
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


def merge_output_frame(
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


def load_completed_pairs(
    *,
    audit_path: Path,
    output_path: Path,
    output_pair_columns: tuple[str, str] = ("ticker", "exchange"),
) -> tuple[set[tuple[str, str]], pd.DataFrame | None, pd.DataFrame | None]:
    completed: set[tuple[str, str]] = set()
    existing_audit: pd.DataFrame | None = None
    existing_output: pd.DataFrame | None = None

    if output_path.exists():
        existing_output = pd.read_parquet(output_path)
        if set(output_pair_columns).issubset(existing_output.columns):
            completed |= set(
                zip(
                    existing_output[output_pair_columns[0]],
                    existing_output[output_pair_columns[1]],
                )
            )

    if audit_path.exists():
        existing_audit = pd.read_csv(audit_path)
        ok_rows = existing_audit[existing_audit["status"].isin(SUCCESS_AUDIT_STATUSES)]
        if {"ticker", "exchange"}.issubset(ok_rows.columns):
            completed |= set(zip(ok_rows["ticker"], ok_rows["exchange"]))

    return completed, existing_audit, existing_output


def merge_audit_rows(
    existing: pd.DataFrame | None, new_rows: list[dict[str, object]]
) -> pd.DataFrame:
    return merge_pair_state_rows(existing, new_rows)


def rebuild_event_audit(
    *,
    output: pd.DataFrame | None,
    state: pd.DataFrame | None,
    existing_audit: pd.DataFrame | None = None,
) -> pd.DataFrame | None:
    records: dict[tuple[str, str], dict[str, object]] = {}

    if (
        existing_audit is not None
        and not existing_audit.empty
        and {"ticker", "exchange"}.issubset(existing_audit.columns)
    ):
        for row in existing_audit.to_dict(orient="records"):
            key = (
                str(row.get("ticker", "")).strip(),
                str(row.get("exchange", "")).strip(),
            )
            if all(key):
                records[key] = row

    state_lookup: dict[tuple[str, str], dict[str, object]] = {}
    if (
        state is not None
        and not state.empty
        and {"ticker", "exchange"}.issubset(state.columns)
    ):
        for row in state.to_dict(orient="records"):
            key = (
                str(row.get("ticker", "")).strip(),
                str(row.get("exchange", "")).strip(),
            )
            if not all(key):
                continue
            state_lookup[key] = row
            latest_data_date = normalize_iso_date(row.get("latest_data_date"))
            coverage_through = normalize_iso_date(row.get("coverage_through"))
            status = str(row.get("status", "")).strip()
            audit_status = status
            if (
                status == "up_to_date"
                and latest_data_date is None
                and coverage_through is not None
            ):
                audit_status = "empty"
            records[key] = {
                "ticker": key[0],
                "exchange": key[1],
                "status": audit_status,
                "n_rows": int(row.get("response_rows", 0) or 0),
                "detail": str(row.get("detail", "") or ""),
                "fetched_at": row.get("fetched_at"),
            }

    if (
        output is not None
        and not output.empty
        and {"ticker", "exchange"}.issubset(output.columns)
    ):
        counts = output.groupby(["ticker", "exchange"], as_index=False).size()
        for ticker, exchange, n_rows in counts.itertuples(index=False, name=None):
            key = (str(ticker).strip(), str(exchange).strip())
            state_row = state_lookup.get(key, {})
            existing_row = records.get(key, {})
            records[key] = {
                "ticker": key[0],
                "exchange": key[1],
                "status": "ok",
                "n_rows": int(n_rows),
                "detail": "",
                "fetched_at": state_row.get(
                    "fetched_at", existing_row.get("fetched_at")
                ),
            }

    if not records:
        return existing_audit

    rebuilt = pd.DataFrame(records.values())
    return rebuilt.sort_values(["exchange", "ticker"]).reset_index(drop=True)
