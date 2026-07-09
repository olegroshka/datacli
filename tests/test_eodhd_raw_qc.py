from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import report_eodhd_raw_quality as qc  # type: ignore  # noqa: E402


def test_load_universe_pairs_falls_back_to_state_when_no_universe_path(
    tmp_path: Path,
) -> None:
    # us_common / uk_eu have no single universe parquet -> derive from prices state.
    pd.DataFrame(
        [
            {"ticker": "vod", "exchange": "lse"},
            {"ticker": "AAPL", "exchange": "US"},
            {"ticker": "AAPL", "exchange": "US"},  # dup collapses
        ]
    ).to_csv(tmp_path / "prices_fetch_state.csv", index=False)

    config = qc.LaneConfig(name="common_test", root=tmp_path, universe_path=None)
    pairs = qc.load_universe_pairs(config)

    assert pairs.to_dict("records") == [
        {"ticker": "AAPL", "exchange": "US"},
        {"ticker": "VOD", "exchange": "LSE"},
    ]


def test_load_universe_pairs_uses_source_exchange_for_uk_eu_etf(tmp_path: Path) -> None:
    universe_path = tmp_path / "tickers_UK_EU_ETF.parquet"
    pd.DataFrame(
        [
            {"Code": "XDAX", "Exchange": "IGNORE", "source_exchange": "XETRA"},
            {"Code": "VUKE", "Exchange": "IGNORE", "source_exchange": "LSE"},
        ]
    ).to_parquet(universe_path, index=False)

    config = qc.LaneConfig(
        name="uk_eu_etf_test",
        root=tmp_path,
        universe_path=universe_path,
        universe_exchange_column="source_exchange",
    )

    pairs = qc.load_universe_pairs(config)

    assert pairs.to_dict("records") == [
        {"ticker": "VUKE", "exchange": "LSE"},
        {"ticker": "XDAX", "exchange": "XETRA"},
    ]


def test_build_price_flags_marks_missing_state_and_invalid_prices() -> None:
    universe = pd.DataFrame(
        [{"ticker": "AAA", "exchange": "US"}, {"ticker": "BBB", "exchange": "US"}]
    )
    metrics = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "row_count": 250,
                "first_date": pd.Timestamp("2024-01-01"),
                "last_date": pd.Timestamp("2024-12-01"),
                "business_days_span": 240,
                "history_density": 1.04,
                "listing_age_days": 400,
                "days_since_last": 40,
                "recent_30_rows": 5,
                "recent_60_rows": 10,
                "recent_252_rows": 100,
                "duplicate_rows": 2,
                "null_ohlc_rows": 0,
                "bad_ohlc_rows": 1,
                "non_positive_rows": 0,
                "negative_volume_rows": 0,
                "zero_volume_recent_60_rows": 0,
                "zero_volume_recent_60_ratio": 0.0,
            }
        ]
    )
    state = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "status": "ok",
                "coverage_through": "2024-12-15",
                "latest_data_date": "2024-11-30",
                "response_rows": 250,
                "fetched_at": "2025-01-01T00:00:00+00:00",
                "detail": "",
            }
        ]
    )
    config = qc.LaneConfig(
        name="us_etf_test",
        root=Path("unused"),
        universe_path=Path("unused"),
        default_exchange="US",
        volume_sensitive=True,
    )

    flags, summary = qc.build_price_flags(
        config,
        universe,
        metrics,
        state,
        as_of_ts=pd.Timestamp("2025-01-10"),
        stale_days=7,
        min_history_days_for_density=365,
        min_recent_252_rows=120,
        min_history_density=0.8,
        min_recent_volume_window=20,
        max_zero_volume_ratio=0.95,
    )

    issues = {(row.ticker, row.issue) for row in flags.itertuples(index=False)}
    assert ("AAA", "duplicate_price_rows") in issues
    assert ("AAA", "invalid_ohlc_relationship") in issues
    assert ("AAA", "state_latest_before_output") in issues
    assert ("AAA", "stale_latest_price") in issues
    assert ("AAA", "sparse_recent_history") in issues
    assert ("BBB", "missing_state") in issues
    assert ("BBB", "missing_price_history") in issues
    assert summary["missing_price_pairs"] == 1
    assert summary["missing_state_pairs"] == 1


def test_build_event_flags_detects_state_mismatches_and_invalid_split_values() -> None:
    universe = pd.DataFrame(
        [{"ticker": "SPLT", "exchange": "US"}, {"ticker": "MISS", "exchange": "US"}]
    )
    event_metrics = pd.DataFrame(
        [
            {
                "ticker": "SPLT",
                "exchange": "US",
                "row_count": 2,
                "first_event_date": pd.Timestamp("2024-01-01"),
                "last_event_date": pd.Timestamp("2024-02-01"),
                "duplicate_rows": 1,
                "invalid_value_rows": 1,
            }
        ]
    )
    state = pd.DataFrame(
        [
            {
                "ticker": "SPLT",
                "exchange": "US",
                "status": "empty",
                "fetched_at": "2025-01-01T00:00:00+00:00",
                "detail": "",
            }
        ]
    )
    audit = pd.DataFrame([{"ticker": "SPLT", "exchange": "US", "status": "ok"}])
    config = qc.LaneConfig(
        name="us_etf_test",
        root=Path("unused"),
        universe_path=Path("unused"),
        default_exchange="US",
        include_events=True,
    )

    flags, summary = qc.build_event_flags(
        config,
        dataset="splits",
        universe_pairs=universe,
        event_metrics=event_metrics,
        state=state,
        audit=audit,
    )

    issues = {(row.ticker, row.issue) for row in flags.itertuples(index=False)}
    assert ("SPLT", "audit_state_mismatch") in issues
    assert ("SPLT", "empty_with_rows") in issues
    assert ("SPLT", "duplicate_event_rows") in issues
    assert ("SPLT", "invalid_event_values") in issues
    assert ("MISS", "missing_state") in issues
    assert ("MISS", "missing_audit_row") in issues
    assert summary["pairs_in_events"] == 1
    assert summary["pairs_in_state"] == 1
    assert summary["pairs_in_audit"] == 1


def test_sort_flags_orders_errors_before_warnings() -> None:
    flags = pd.DataFrame(
        [
            {
                "lane": "x",
                "dataset": "prices",
                "ticker": "BBB",
                "exchange": "US",
                "severity": "warning",
                "issue": "warn",
                "recommendation": "manual_inspect",
                "detail": "b",
            },
            {
                "lane": "x",
                "dataset": "prices",
                "ticker": "AAA",
                "exchange": "US",
                "severity": "error",
                "issue": "err",
                "recommendation": "targeted_rerun",
                "detail": "a",
            },
        ]
    )

    ordered = qc.sort_flags(flags)

    assert ordered.iloc[0]["severity"] == "error"
    assert ordered.iloc[0]["ticker"] == "AAA"
