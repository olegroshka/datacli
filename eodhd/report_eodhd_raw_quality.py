"""Quality checks for raw EODHD ETF and index-reference datasets.

The report focuses on operator-actionable issues:

- universe/state/output mismatches,
- stale or sparse price histories,
- structurally invalid price rows,
- event sidecar consistency,
- and suspicious dividend/split payload integrity.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from _datadir import EODHD_RAW_ROOT

_ROOT = Path(__file__).resolve().parents[2]
_RAW_EODHD = EODHD_RAW_ROOT


@dataclass(frozen=True)
class LaneConfig:
    name: str
    root: Path
    universe_path: Path | None = None
    universe_code_column: str = "Code"
    universe_exchange_column: str | None = None
    default_exchange: str | None = None
    volume_sensitive: bool = True
    include_events: bool = True


LANES: dict[str, LaneConfig] = {
    "us_common": LaneConfig(
        name="us_common",
        root=_RAW_EODHD / "us_common",
        universe_path=None,  # qualifying subset -> derive from prices_fetch_state.csv
        default_exchange="US",
        volume_sensitive=True,
        include_events=True,
    ),
    "uk_eu": LaneConfig(
        name="uk_eu",
        root=_RAW_EODHD / "uk_eu",
        universe_path=None,  # universe split across tickers_* files -> use state
        volume_sensitive=True,
        include_events=True,
    ),
    "us_etf": LaneConfig(
        name="us_etf",
        root=_RAW_EODHD / "us_etf",
        universe_path=_RAW_EODHD / "us_etf" / "tickers_US_ETF.parquet",
        default_exchange="US",
        volume_sensitive=True,
        include_events=True,
    ),
    "index_ref": LaneConfig(
        name="index_ref",
        root=_RAW_EODHD / "index_ref",
        universe_path=_RAW_EODHD / "index_ref" / "tickers_INDX.parquet",
        default_exchange="INDX",
        volume_sensitive=False,
        include_events=False,
    ),
    "uk_eu_etf": LaneConfig(
        name="uk_eu_etf",
        root=_RAW_EODHD / "uk_eu_etf",
        universe_path=_RAW_EODHD / "uk_eu_etf" / "tickers_UK_EU_ETF.parquet",
        universe_exchange_column="source_exchange",
        volume_sensitive=True,
        include_events=True,
    ),
    "uk_eu_index_ref": LaneConfig(
        name="uk_eu_index_ref",
        root=_RAW_EODHD / "uk_eu_index_ref",
        universe_path=_RAW_EODHD / "uk_eu_index_ref" / "tickers_INDX_UK_EU.parquet",
        default_exchange="INDX",
        volume_sensitive=False,
        include_events=False,
    ),
}

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report quality checks for raw EODHD ETF and index-reference data"
    )
    parser.add_argument("--lane", choices=[*LANES.keys(), "all"], default="all")
    parser.add_argument(
        "--as-of",
        dest="as_of",
        default=date.today().isoformat(),
        help="Audit date (YYYY-MM-DD)",
    )
    parser.add_argument(
        "--stale-days",
        type=int,
        default=7,
        help="Flag price histories older than this many days",
    )
    parser.add_argument(
        "--min-history-days-for-density",
        type=int,
        default=365,
        help="Require at least this listing age before low-density price histories are flagged",
    )
    parser.add_argument(
        "--min-recent-252-rows",
        type=int,
        default=120,
        help="Flag older price histories with fewer than this many rows in the recent 252-day window",
    )
    parser.add_argument(
        "--min-history-density",
        type=float,
        default=0.60,
        help="Flag older price histories whose row-count/business-day density falls below this threshold",
    )
    parser.add_argument(
        "--min-recent-volume-window",
        type=int,
        default=20,
        help="Require at least this many recent bars before zero-volume ratio checks trigger",
    )
    parser.add_argument(
        "--max-zero-volume-ratio",
        type=float,
        default=0.95,
        help="Flag ETF histories whose recent zero-volume share exceeds this threshold",
    )
    parser.add_argument(
        "--max-flags-per-lane",
        type=int,
        default=8,
        help="Maximum number of per-lane flags to print in the console summary",
    )
    parser.add_argument(
        "--write-report",
        action="store_true",
        help="Persist `qc_summary.json` and `qc_flags.csv` into each audited lane root",
    )
    return parser.parse_args()


def load_universe_pairs(config: LaneConfig) -> pd.DataFrame:
    if config.universe_path is None:
        # Lanes without a single universe parquet (uk_eu is split across files;
        # us_common's qualifying subset differs from the ticker master) derive
        # their expected universe from the prices fetch-state sidecar.
        state = read_state_frame(config.root / "prices_fetch_state.csv")
        pairs = state[["ticker", "exchange"]].copy()
        return (
            pairs.dropna()
            .drop_duplicates()
            .sort_values(["ticker", "exchange"])
            .reset_index(drop=True)
        )
    universe = pd.read_parquet(config.universe_path)
    exchange_column = config.universe_exchange_column
    if exchange_column and exchange_column in universe.columns:
        exchanges = universe[exchange_column].fillna(universe.get("Exchange"))
        pairs = pd.DataFrame(
            {
                "ticker": universe[config.universe_code_column]
                .astype(str)
                .str.strip()
                .str.upper(),
                "exchange": exchanges.astype(str).str.strip().str.upper(),
            }
        )
    else:
        if config.default_exchange is None:
            raise ValueError(
                f"Lane {config.name} requires either `default_exchange` or `universe_exchange_column`"
            )
        pairs = pd.DataFrame(
            {
                "ticker": universe[config.universe_code_column]
                .astype(str)
                .str.strip()
                .str.upper(),
                "exchange": config.default_exchange,
            }
        )
    pairs = (
        pairs.dropna()
        .drop_duplicates()
        .sort_values(["ticker", "exchange"])
        .reset_index(drop=True)
    )
    return pairs


def normalize_pair_columns(frame: pd.DataFrame) -> pd.DataFrame:
    normalized = frame.copy()
    normalized["ticker"] = normalized["ticker"].astype(str).str.strip().str.upper()
    normalized["exchange"] = normalized["exchange"].astype(str).str.strip().str.upper()
    return normalized


def read_state_frame(path: Path) -> pd.DataFrame:
    state = pd.read_csv(path)
    state = normalize_pair_columns(state)
    return state


def parse_as_of_timestamp(value: str) -> pd.Timestamp:
    as_of_ts = pd.Timestamp(value)
    if pd.isna(as_of_ts):
        raise ValueError(f"Invalid --as-of value: {value}")
    return pd.Timestamp(as_of_ts)


def as_int(value: Any, default: int = 0) -> int:
    return default if pd.isna(value) else int(value)


def as_float(value: Any) -> float | None:
    return None if pd.isna(value) else float(value)


def as_str(value: Any) -> str | None:
    return None if pd.isna(value) else str(value)


def as_timestamp(value: Any) -> pd.Timestamp | None:
    if pd.isna(value):
        return None
    return pd.Timestamp(value)


def build_price_metrics(
    prices: pd.DataFrame, *, as_of_ts: pd.Timestamp, volume_sensitive: bool
) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame(
            columns=[
                "ticker",
                "exchange",
                "row_count",
                "first_date",
                "last_date",
                "business_days_span",
                "history_density",
                "listing_age_days",
                "days_since_last",
                "recent_30_rows",
                "recent_60_rows",
                "recent_252_rows",
                "duplicate_rows",
                "null_ohlc_rows",
                "bad_ohlc_rows",
                "non_positive_rows",
                "negative_volume_rows",
                "zero_volume_recent_60_rows",
                "zero_volume_recent_60_ratio",
            ]
        )

    frame = normalize_pair_columns(prices)
    frame["date"] = pd.to_datetime(frame["date"])
    group_keys = [frame["ticker"], frame["exchange"]]
    grouped = frame.groupby(["ticker", "exchange"], sort=False)

    metrics = grouped.agg(
        row_count=("date", "size"),
        first_date=("date", "min"),
        last_date=("date", "max"),
    ).reset_index()

    duplicate_rows = (
        frame.duplicated(["ticker", "exchange", "date"])
        .groupby(group_keys)
        .sum()
        .rename("duplicate_rows")
    )
    null_ohlc_rows = (
        frame[["open", "high", "low", "close", "adjusted_close"]]
        .isna()
        .any(axis=1)
        .groupby(group_keys)
        .sum()
        .rename("null_ohlc_rows")
    )
    bad_ohlc_mask = (
        (frame["high"] < frame[["open", "close", "low"]].max(axis=1))
        | (frame["low"] > frame[["open", "close", "high"]].min(axis=1))
    ).fillna(False)
    bad_ohlc_rows = bad_ohlc_mask.groupby(group_keys).sum().rename("bad_ohlc_rows")
    non_positive_mask = (
        (frame["open"] <= 0)
        | (frame["high"] <= 0)
        | (frame["low"] <= 0)
        | (frame["close"] <= 0)
        | (frame["adjusted_close"] <= 0)
    ).fillna(False)
    non_positive_rows = (
        non_positive_mask.groupby(group_keys).sum().rename("non_positive_rows")
    )
    negative_volume_rows = (
        (frame["volume"] < 0)
        .fillna(False)
        .groupby(group_keys)
        .sum()
        .rename("negative_volume_rows")
    )

    recent_30_rows = (
        (frame["date"] >= (as_of_ts - pd.Timedelta(days=30)))
        .groupby(group_keys)
        .sum()
        .rename("recent_30_rows")
    )
    recent_60_mask = frame["date"] >= (as_of_ts - pd.Timedelta(days=60))
    recent_60_rows = recent_60_mask.groupby(group_keys).sum().rename("recent_60_rows")
    recent_252_rows = (
        (frame["date"] >= (as_of_ts - pd.Timedelta(days=252)))
        .groupby(group_keys)
        .sum()
        .rename("recent_252_rows")
    )

    if volume_sensitive:
        zero_volume_recent_60_rows = (
            ((frame["volume"] == 0) & recent_60_mask)
            .fillna(False)
            .groupby(group_keys)
            .sum()
            .rename("zero_volume_recent_60_rows")
        )
    else:
        zero_volume_recent_60_rows = pd.Series(
            0, index=recent_60_rows.index, name="zero_volume_recent_60_rows"
        )

    metrics = metrics.merge(
        duplicate_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        null_ohlc_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        bad_ohlc_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        non_positive_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        negative_volume_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        recent_30_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        recent_60_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        recent_252_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        zero_volume_recent_60_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )

    metrics["business_days_span"] = [
        (
            len(pd.bdate_range(first_date, last_date))
            if pd.notna(first_date) and pd.notna(last_date)
            else 0
        )
        for first_date, last_date in zip(metrics["first_date"], metrics["last_date"])
    ]
    metrics["history_density"] = metrics["row_count"] / metrics[
        "business_days_span"
    ].replace(0, pd.NA)
    metrics["listing_age_days"] = (as_of_ts - metrics["first_date"]).dt.days
    metrics["days_since_last"] = (as_of_ts - metrics["last_date"]).dt.days
    metrics["zero_volume_recent_60_ratio"] = metrics[
        "zero_volume_recent_60_rows"
    ] / metrics["recent_60_rows"].replace(0, pd.NA)
    return metrics


def build_event_metrics(events: pd.DataFrame, *, event_type: str) -> pd.DataFrame:
    if events.empty:
        columns = [
            "ticker",
            "exchange",
            "row_count",
            "first_event_date",
            "last_event_date",
            "duplicate_rows",
        ]
        if event_type == "dividends":
            columns.extend(["invalid_value_rows"])
        else:
            columns.extend(["invalid_value_rows"])
        return pd.DataFrame(columns=columns)

    frame = normalize_pair_columns(events)
    frame["ex_date"] = pd.to_datetime(frame["ex_date"])
    grouped = frame.groupby(["ticker", "exchange"], sort=False)
    metrics = grouped.agg(
        row_count=("ex_date", "size"),
        first_event_date=("ex_date", "min"),
        last_event_date=("ex_date", "max"),
    ).reset_index()

    if event_type == "dividends":
        duplicate_mask = frame.duplicated(
            ["ticker", "exchange", "ex_date", "dividend", "unadjusted_dividend"]
        )
        invalid_mask = (
            (frame["dividend"] < 0) | (frame["unadjusted_dividend"] < 0)
        ).fillna(False)
    else:
        duplicate_mask = frame.duplicated(
            ["ticker", "exchange", "ex_date", "split_factor"]
        )
        invalid_mask = (
            (frame["split_ratio"] <= 0)
            | (frame["numerator"] <= 0)
            | (frame["denominator"] <= 0)
        ).fillna(False)

    group_keys = [frame["ticker"], frame["exchange"]]
    duplicate_rows = duplicate_mask.groupby(group_keys).sum().rename("duplicate_rows")
    invalid_rows = invalid_mask.groupby(group_keys).sum().rename("invalid_value_rows")

    metrics = metrics.merge(
        duplicate_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    metrics = metrics.merge(
        invalid_rows.reset_index(), on=["ticker", "exchange"], how="left"
    )
    return metrics


def build_flag(
    *,
    lane: str,
    dataset: str,
    ticker: str,
    exchange: str,
    severity: str,
    issue: str,
    recommendation: str,
    detail: str,
) -> dict[str, Any]:
    return {
        "lane": lane,
        "dataset": dataset,
        "ticker": ticker,
        "exchange": exchange,
        "severity": severity,
        "issue": issue,
        "recommendation": recommendation,
        "detail": detail,
    }


def build_price_flags(
    config: LaneConfig,
    universe_pairs: pd.DataFrame,
    price_metrics: pd.DataFrame,
    state: pd.DataFrame | None,
    *,
    as_of_ts: pd.Timestamp,
    stale_days: int,
    min_history_days_for_density: int,
    min_recent_252_rows: int,
    min_history_density: float,
    min_recent_volume_window: int,
    max_zero_volume_ratio: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    state_subset = None
    if state is not None:
        state_subset = state[
            [
                "ticker",
                "exchange",
                "status",
                "coverage_through",
                "latest_data_date",
                "response_rows",
                "fetched_at",
                "detail",
            ]
        ].copy()
        for column in ["coverage_through", "latest_data_date"]:
            state_subset[column] = pd.to_datetime(state_subset[column], errors="coerce")

    merged = universe_pairs.copy()
    merged = merged.merge(price_metrics, on=["ticker", "exchange"], how="left")
    if state_subset is not None:
        merged = merged.merge(state_subset, on=["ticker", "exchange"], how="left")
    else:
        merged["status"] = None
        merged["coverage_through"] = pd.NaT
        merged["latest_data_date"] = pd.NaT
        merged["response_rows"] = pd.NA
        merged["fetched_at"] = None
        merged["detail"] = None

    flags: list[dict[str, Any]] = []
    allowed_statuses = {"ok", "up_to_date"}

    for row in merged.to_dict("records"):
        ticker = str(row["ticker"])
        exchange = str(row["exchange"])
        status = as_str(row.get("status"))
        row_count = as_int(row.get("row_count"))
        business_days_span = as_int(row.get("business_days_span"))
        history_density = as_float(row.get("history_density"))
        listing_age_days = (
            None
            if pd.isna(row.get("listing_age_days"))
            else as_int(row.get("listing_age_days"))
        )
        days_since_last = (
            None
            if pd.isna(row.get("days_since_last"))
            else as_int(row.get("days_since_last"))
        )
        recent_60_rows = as_int(row.get("recent_60_rows"))
        recent_252_rows = as_int(row.get("recent_252_rows"))
        zero_volume_recent_60_ratio = as_float(row.get("zero_volume_recent_60_ratio"))

        if status is None:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="missing_state",
                    recommendation="targeted_rerun",
                    detail="Pair missing from prices_fetch_state.csv",
                )
            )
        elif status not in allowed_statuses:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="bad_state_status",
                    recommendation="targeted_rerun",
                    detail=f"Unexpected state status: {status}",
                )
            )

        if row_count == 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="missing_price_history",
                    recommendation="targeted_rerun",
                    detail="Pair missing from prices_daily.parquet",
                )
            )
            continue

        duplicate_rows = as_int(row.get("duplicate_rows"))
        null_ohlc_rows = as_int(row.get("null_ohlc_rows"))
        bad_ohlc_rows = as_int(row.get("bad_ohlc_rows"))
        non_positive_rows = as_int(row.get("non_positive_rows"))
        negative_volume_rows = as_int(row.get("negative_volume_rows"))

        if duplicate_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="duplicate_price_rows",
                    recommendation="full_refresh",
                    detail=f"Duplicate date rows: {duplicate_rows}",
                )
            )
        if null_ohlc_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="null_ohlc_rows",
                    recommendation="full_refresh",
                    detail=f"Rows with null OHLC fields: {null_ohlc_rows}",
                )
            )
        if bad_ohlc_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="invalid_ohlc_relationship",
                    recommendation="full_refresh",
                    detail=f"Rows with invalid high/low relationships: {bad_ohlc_rows}",
                )
            )
        if non_positive_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="non_positive_prices",
                    recommendation="full_refresh",
                    detail=f"Rows with non-positive prices: {non_positive_rows}",
                )
            )
        if negative_volume_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="negative_volume_rows",
                    recommendation="full_refresh",
                    detail=f"Rows with negative volume: {negative_volume_rows}",
                )
            )

        latest_data_date = as_timestamp(row.get("latest_data_date"))
        coverage_through = as_timestamp(row.get("coverage_through"))
        last_date = as_timestamp(row.get("last_date"))

        if (
            latest_data_date is not None
            and last_date is not None
            and latest_data_date.date() < last_date.date()
        ):
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="state_latest_before_output",
                    recommendation="incremental_rerun",
                    detail=f"latest_data_date={latest_data_date.date()} < last_date={last_date.date()}",
                )
            )
        if (
            coverage_through is not None
            and last_date is not None
            and coverage_through.date() < last_date.date()
        ):
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="coverage_before_output",
                    recommendation="incremental_rerun",
                    detail=f"coverage_through={coverage_through.date()} < last_date={last_date.date()}",
                )
            )

        if (
            days_since_last is not None
            and listing_age_days is not None
            and listing_age_days >= 60
            and days_since_last > stale_days
        ):
            recommendation = (
                "manual_inspect" if days_since_last > 90 else "incremental_rerun"
            )
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="stale_latest_price",
                    recommendation=recommendation,
                    detail=f"days_since_last={days_since_last}",
                )
            )

        if (
            listing_age_days is not None
            and listing_age_days >= min_history_days_for_density
        ):
            if (
                history_density is not None
                and business_days_span > 0
                and history_density < min_history_density
                and (
                    (days_since_last is not None and days_since_last > stale_days)
                    or recent_252_rows < min_recent_252_rows
                )
            ):
                flags.append(
                    build_flag(
                        lane=config.name,
                        dataset="prices",
                        ticker=ticker,
                        exchange=exchange,
                        severity="warning",
                        issue="low_history_density",
                        recommendation="targeted_rerun",
                        detail=f"density={history_density:.3f} over {business_days_span} business days",
                    )
                )
            if recent_252_rows < min_recent_252_rows:
                flags.append(
                    build_flag(
                        lane=config.name,
                        dataset="prices",
                        ticker=ticker,
                        exchange=exchange,
                        severity="warning",
                        issue="sparse_recent_history",
                        recommendation="targeted_rerun",
                        detail=f"recent_252_rows={recent_252_rows}",
                    )
                )

        if (
            config.volume_sensitive
            and recent_60_rows >= min_recent_volume_window
            and zero_volume_recent_60_ratio is not None
            and zero_volume_recent_60_ratio > max_zero_volume_ratio
        ):
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset="prices",
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="mostly_zero_recent_volume",
                    recommendation="manual_inspect",
                    detail=f"zero_volume_recent_60_ratio={zero_volume_recent_60_ratio:.3f}",
                )
            )

    flags_df = pd.DataFrame(flags)
    summary = {
        "universe_pairs": int(len(universe_pairs)),
        "pairs_in_prices": int(len(price_metrics)),
        "pairs_in_state": (
            0
            if state is None
            else int(state[["ticker", "exchange"]].drop_duplicates().shape[0])
        ),
        "missing_price_pairs": int((merged["row_count"].fillna(0) == 0).sum()),
        "missing_state_pairs": int(merged["status"].isna().sum()),
        "state_status_counts": (
            {}
            if state is None or "status" not in state.columns
            else {
                str(k): int(v)
                for k, v in state["status"].value_counts().to_dict().items()
            }
        ),
    }
    return flags_df, summary


def build_event_flags(
    config: LaneConfig,
    *,
    dataset: str,
    universe_pairs: pd.DataFrame,
    event_metrics: pd.DataFrame,
    state: pd.DataFrame | None,
    audit: pd.DataFrame | None,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    state_subset = None
    if state is not None:
        state_subset = state[
            ["ticker", "exchange", "status", "fetched_at", "detail"]
        ].copy()
    audit_subset = None
    if audit is not None:
        audit_subset = (
            audit[["ticker", "exchange", "status"]]
            .copy()
            .rename(columns={"status": "audit_status"})
        )

    merged = universe_pairs.copy().merge(
        event_metrics, on=["ticker", "exchange"], how="left"
    )
    if state_subset is not None:
        merged = merged.merge(state_subset, on=["ticker", "exchange"], how="left")
    else:
        merged["status"] = None
        merged["fetched_at"] = None
        merged["detail"] = None
    if audit_subset is not None:
        merged = merged.merge(audit_subset, on=["ticker", "exchange"], how="left")
    else:
        merged["audit_status"] = None

    flags: list[dict[str, Any]] = []
    allowed_statuses = {"ok", "empty"}

    for row in merged.to_dict("records"):
        ticker = str(row["ticker"])
        exchange = str(row["exchange"])
        status = as_str(row.get("status"))
        audit_status = as_str(row.get("audit_status"))
        row_count = as_int(row.get("row_count"))
        duplicate_rows = as_int(row.get("duplicate_rows"))
        invalid_value_rows = as_int(row.get("invalid_value_rows"))

        if status is None:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="missing_state",
                    recommendation="targeted_rerun",
                    detail=f"Pair missing from {dataset}_fetch_state.csv",
                )
            )
        elif status not in allowed_statuses:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="bad_state_status",
                    recommendation="targeted_rerun",
                    detail=f"Unexpected state status: {status}",
                )
            )

        if audit is not None and audit_status is None:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="missing_audit_row",
                    recommendation="targeted_rerun",
                    detail=f"Pair missing from {dataset}_fetch_audit.csv",
                )
            )
        elif audit_status is not None and status is not None and audit_status != status:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="warning",
                    issue="audit_state_mismatch",
                    recommendation="targeted_rerun",
                    detail=f"state={status}, audit={audit_status}",
                )
            )

        if status == "ok" and row_count == 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="ok_without_rows",
                    recommendation="targeted_rerun",
                    detail="State says ok but event parquet has no rows",
                )
            )
        if status == "empty" and row_count > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="empty_with_rows",
                    recommendation="full_refresh",
                    detail=f"State says empty but event parquet has {row_count} rows",
                )
            )
        if duplicate_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="duplicate_event_rows",
                    recommendation="full_refresh",
                    detail=f"Duplicate event rows: {duplicate_rows}",
                )
            )
        if invalid_value_rows > 0:
            flags.append(
                build_flag(
                    lane=config.name,
                    dataset=dataset,
                    ticker=ticker,
                    exchange=exchange,
                    severity="error",
                    issue="invalid_event_values",
                    recommendation="full_refresh",
                    detail=f"Rows with invalid event values: {invalid_value_rows}",
                )
            )

    flags_df = pd.DataFrame(flags)
    summary = {
        "pairs_in_events": int(len(event_metrics)),
        "pairs_in_state": (
            0
            if state is None
            else int(state[["ticker", "exchange"]].drop_duplicates().shape[0])
        ),
        "pairs_in_audit": (
            0
            if audit is None
            else int(audit[["ticker", "exchange"]].drop_duplicates().shape[0])
        ),
        "state_status_counts": (
            {}
            if state is None or "status" not in state.columns
            else {
                str(k): int(v)
                for k, v in state["status"].value_counts().to_dict().items()
            }
        ),
        "audit_status_counts": (
            {}
            if audit is None or "status" not in audit.columns
            else {
                str(k): int(v)
                for k, v in audit["status"].value_counts().to_dict().items()
            }
        ),
    }
    return flags_df, summary


def summarize_flags(flags: pd.DataFrame) -> dict[str, int]:
    if flags.empty:
        return {}
    return {
        str(key): int(value)
        for key, value in flags["severity"].value_counts().to_dict().items()
    }


def summarize_issues(flags: pd.DataFrame) -> dict[str, int]:
    if flags.empty:
        return {}
    return {
        str(key): int(value)
        for key, value in flags["issue"].value_counts().to_dict().items()
    }


def sort_flags(flags: pd.DataFrame) -> pd.DataFrame:
    if flags.empty:
        return flags
    ordered = flags.copy()
    ordered["severity_rank"] = ordered["severity"].map(SEVERITY_ORDER).fillna(99)
    ordered = ordered.sort_values(
        ["severity_rank", "dataset", "issue", "ticker", "exchange"]
    ).drop(columns=["severity_rank"])
    return ordered.reset_index(drop=True)


def audit_lane(
    config: LaneConfig, args: argparse.Namespace
) -> tuple[dict[str, Any], pd.DataFrame]:
    as_of_ts = parse_as_of_timestamp(args.as_of)
    universe_pairs = load_universe_pairs(config)

    prices_path = config.root / "prices_daily.parquet"
    prices = pd.read_parquet(
        prices_path,
        columns=[
            "date",
            "open",
            "high",
            "low",
            "close",
            "adjusted_close",
            "volume",
            "ticker",
            "exchange",
        ],
    )
    price_metrics = build_price_metrics(
        prices, as_of_ts=as_of_ts, volume_sensitive=config.volume_sensitive
    )
    price_state = (
        read_state_frame(config.root / "prices_fetch_state.csv")
        if (config.root / "prices_fetch_state.csv").exists()
        else None
    )
    price_flags, price_summary = build_price_flags(
        config,
        universe_pairs,
        price_metrics,
        price_state,
        as_of_ts=as_of_ts,
        stale_days=args.stale_days,
        min_history_days_for_density=args.min_history_days_for_density,
        min_recent_252_rows=args.min_recent_252_rows,
        min_history_density=args.min_history_density,
        min_recent_volume_window=args.min_recent_volume_window,
        max_zero_volume_ratio=args.max_zero_volume_ratio,
    )

    all_flags = [price_flags]
    datasets: dict[str, Any] = {
        "prices": {
            **price_summary,
            "rows": int(len(prices)),
            "date_min": str(prices["date"].min()),
            "date_max": str(prices["date"].max()),
            "flag_counts": summarize_flags(price_flags),
        }
    }

    if config.include_events:
        for dataset in ["dividends", "splits"]:
            history_path = config.root / f"{dataset}_history.parquet"
            state_path = config.root / f"{dataset}_fetch_state.csv"
            audit_path = config.root / f"{dataset}_fetch_audit.csv"
            history = pd.read_parquet(history_path)
            metrics = build_event_metrics(history, event_type=dataset)
            state = read_state_frame(state_path) if state_path.exists() else None
            audit = read_state_frame(audit_path) if audit_path.exists() else None
            event_flags, event_summary = build_event_flags(
                config,
                dataset=dataset,
                universe_pairs=universe_pairs,
                event_metrics=metrics,
                state=state,
                audit=audit,
            )
            all_flags.append(event_flags)
            datasets[dataset] = {
                **event_summary,
                "rows": int(len(history)),
                "event_date_min": (
                    None if history.empty else str(history["ex_date"].min())
                ),
                "event_date_max": (
                    None if history.empty else str(history["ex_date"].max())
                ),
                "flag_counts": summarize_flags(event_flags),
            }

    flags = sort_flags(
        pd.concat([frame for frame in all_flags if not frame.empty], ignore_index=True)
        if any(not frame.empty for frame in all_flags)
        else pd.DataFrame(
            columns=[
                "lane",
                "dataset",
                "ticker",
                "exchange",
                "severity",
                "issue",
                "recommendation",
                "detail",
            ]
        )
    )
    summary = {
        "lane": config.name,
        "as_of": args.as_of,
        "datasets": datasets,
        "total_flag_counts": summarize_flags(flags),
        "issue_counts": summarize_issues(flags),
        "total_flags": int(len(flags)),
    }
    return summary, flags


def print_lane_summary(
    summary: dict[str, Any], flags: pd.DataFrame, *, max_flags: int
) -> None:
    print(f"\n== {summary['lane']} ==")
    prices = summary["datasets"]["prices"]
    print(
        "prices: "
        f"universe={prices['universe_pairs']}, output_pairs={prices['pairs_in_prices']}, state_pairs={prices['pairs_in_state']}, "
        f"rows={prices['rows']:,}, range={prices['date_min']} -> {prices['date_max']}"
    )
    print(
        "price state: "
        + ", ".join(f"{k}={v}" for k, v in prices["state_status_counts"].items())
        + (
            f"; flags={prices['flag_counts']}"
            if prices["flag_counts"]
            else "; flags={}"
        )
    )

    for dataset in ["dividends", "splits"]:
        if dataset not in summary["datasets"]:
            continue
        meta = summary["datasets"][dataset]
        print(
            f"{dataset}: pairs_in_events={meta['pairs_in_events']}, state_pairs={meta['pairs_in_state']}, audit_pairs={meta['pairs_in_audit']}, rows={meta['rows']:,}"
        )
        print(
            f"{dataset} state: "
            + ", ".join(f"{k}={v}" for k, v in meta["state_status_counts"].items())
            + (
                f"; flags={meta['flag_counts']}"
                if meta["flag_counts"]
                else "; flags={}"
            )
        )

    if flags.empty:
        print("flags: none")
        return

    print(f"flags: {summary['total_flag_counts']}")
    if summary["issue_counts"]:
        top_issues = list(summary["issue_counts"].items())[:5]
        print(
            "top issues: "
            + ", ".join(f"{issue}={count}" for issue, count in top_issues)
        )
    top_flags = flags.head(max_flags)
    for row in top_flags.itertuples(index=False):
        print(
            f"- [{row.severity}] {row.dataset} {row.ticker}.{row.exchange}: {row.issue} -> {row.recommendation} ({row.detail})"
        )
    remaining = len(flags) - len(top_flags)
    if remaining > 0:
        print(f"- ... {remaining} additional flags not shown")


def maybe_write_outputs(
    config: LaneConfig, summary: dict[str, Any], flags: pd.DataFrame
) -> None:
    config.root.mkdir(parents=True, exist_ok=True)
    (config.root / "qc_summary.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    flags.to_csv(config.root / "qc_flags.csv", index=False)


def main() -> None:
    args = parse_args()
    lane_names = list(LANES) if args.lane == "all" else [args.lane]

    any_flags = False
    for lane_name in lane_names:
        config = LANES[lane_name]
        summary, flags = audit_lane(config, args)
        print_lane_summary(summary, flags, max_flags=args.max_flags_per_lane)
        if args.write_report:
            maybe_write_outputs(config, summary, flags)
        any_flags = any_flags or not flags.empty

    if args.write_report:
        print("\nWrote qc_summary.json / qc_flags.csv into each audited lane root.")

    if any_flags:
        print(
            "\nQC report finished with actionable flags. Review the per-lane summaries above."
        )
    else:
        print("\nQC report finished with no flags.")


if __name__ == "__main__":
    main()
