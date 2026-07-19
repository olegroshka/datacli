from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_bulk as bulk  # type: ignore  # noqa: E402


def test_parse_split() -> None:
    assert bulk.parse_split("2.000000/1.000000") == (2.0, 1.0, 2.0)
    assert bulk.parse_split("10/1") == (10.0, 1.0, 10.0)
    assert bulk.parse_split(None) == (None, None, None)
    assert bulk.parse_split("bad") == (None, None, None)


def test_normalize_prices_maps_bulk_schema() -> None:
    rows = [
        {
            "code": "aapl",
            "exchange_short_name": "US",
            "date": "2026-07-08",
            "open": 1,
            "high": 2,
            "low": 0.5,
            "close": 1.5,
            "adjusted_close": 1.5,
            "volume": 100,
        }
    ]
    df = bulk.normalize_prices(rows)
    assert list(df.columns) == bulk.PRICE_COLUMNS
    r = df.iloc[0].to_dict()
    assert r["ticker"] == "AAPL" and r["exchange"] == "US" and r["close"] == 1.5


def test_normalize_splits_parses_ratio() -> None:
    df = bulk.normalize_splits(
        [
            {
                "code": "ZWS",
                "exchange": "US",
                "date": "2026-07-08",
                "split": "3.000000/1.000000",
            }
        ]
    )
    r = df.iloc[0].to_dict()
    assert (r["numerator"], r["denominator"], r["split_ratio"]) == (3.0, 1.0, 3.0)
    assert r["split_factor"] == "3.000000/1.000000"


def test_gap_dates_business_days() -> None:
    state = pd.DataFrame(
        {"ticker": ["A"], "exchange": ["US"], "coverage_through": ["2026-07-06"]}
    )
    # 2026-07-06 Mon covered; as-of Wed 2026-07-08 -> fetch Tue+Wed
    dates = bulk.gap_dates(state, pd.Timestamp("2026-07-08"), max_days=7)
    assert dates == ["2026-07-07", "2026-07-08"]
    # already covered -> nothing
    state2 = pd.DataFrame(
        {"ticker": ["A"], "exchange": ["US"], "coverage_through": ["2026-07-08"]}
    )
    assert bulk.gap_dates(state2, pd.Timestamp("2026-07-08"), max_days=7) == []


def test_gap_dates_uses_latest_data_date_when_coverage_runs_ahead() -> None:
    # Regression: a fetch before the day's close records latest_data_date=07-07
    # but sets coverage_through=07-08. Keying the gap off coverage_through would
    # skip 07-08; keying off latest_data_date (prices' freshness_col) catches it.
    state = pd.DataFrame(
        {
            "ticker": ["A"],
            "exchange": ["US"],
            "coverage_through": ["2026-07-08"],
            "latest_data_date": ["2026-07-07"],
        }
    )
    as_of = pd.Timestamp("2026-07-08")
    assert (
        bulk.gap_dates(state, as_of, 7, freshness_col="coverage_through") == []
    )  # old bug
    assert bulk.gap_dates(state, as_of, 7, freshness_col="latest_data_date") == [
        "2026-07-08"
    ]


def test_select_new_rows_filters_and_skips_big_gaps() -> None:
    normalized = pd.DataFrame(
        [
            {
                "ticker": "A",
                "exchange": "US",
                "date": "2026-07-08",
                "close": 1,
            },  # new for A
            {
                "ticker": "A",
                "exchange": "US",
                "date": "2026-07-06",
                "close": 1,
            },  # <= coverage, drop
            {
                "ticker": "B",
                "exchange": "US",
                "date": "2026-07-08",
                "close": 1,
            },  # B gap too big -> skip
            {
                "ticker": "Z",
                "exchange": "US",
                "date": "2026-07-08",
                "close": 1,
            },  # not in universe
        ]
    )
    coverage = {
        ("A", "US"): pd.Timestamp("2026-07-06"),
        ("B", "US"): pd.Timestamp("2026-01-01"),  # far behind
    }
    new_rows, skipped = bulk.select_new_rows(
        normalized,
        coverage,
        date_col="date",
        earliest_fetched=pd.Timestamp("2026-07-07"),
    )
    assert set(zip(new_rows["ticker"], new_rows["date"])) == {("A", "2026-07-08")}
    assert skipped == {("B", "US")}


def test_merge_output_appends_new_preserves_existing() -> None:
    existing = pd.DataFrame(
        [{"ticker": "A", "exchange": "US", "date": "2026-07-06", "close": 10.0}]
    )
    new = pd.DataFrame(
        [
            {
                "ticker": "A",
                "exchange": "US",
                "date": "2026-07-06",
                "close": 11.0,
            },  # dup key
            {
                "ticker": "A",
                "exchange": "US",
                "date": "2026-07-07",
                "close": 12.0,
            },  # new
        ]
    )
    merged = bulk.merge_output(existing, new, ["ticker", "exchange", "date"])
    assert set(merged["date"]) == {"2026-07-06", "2026-07-07"}
    # existing row is preserved untouched (not overwritten by the dup-key new row)
    assert merged[merged["date"] == "2026-07-06"].iloc[0]["close"] == 10.0


def test_merge_output_preserves_multi_dividend_same_exdate() -> None:
    # Regression guard: a firm can pay two distinct dividends on one ex-date;
    # keying on ex_date alone would collapse them and lose data.
    keys = ["ticker", "exchange", "ex_date", "dividend", "unadjusted_dividend"]
    existing = pd.DataFrame(
        [
            {
                "ticker": "AAAD",
                "exchange": "US",
                "ex_date": "2026-06-30",
                "dividend": 0.016,
                "unadjusted_dividend": 0.016,
            }
        ]
    )
    new = pd.DataFrame(
        [
            {
                "ticker": "AAAD",
                "exchange": "US",
                "ex_date": "2026-06-30",
                "dividend": 0.016,
                "unadjusted_dividend": 0.016,
            },  # exact dup -> skip
            {
                "ticker": "AAAD",
                "exchange": "US",
                "ex_date": "2026-06-30",
                "dividend": 0.01644,
                "unadjusted_dividend": 0.01644,
            },  # distinct -> add
        ]
    )
    merged = bulk.merge_output(existing, new, keys)
    assert len(merged) == 2  # both distinct dividends kept


def test_advance_state() -> None:
    state = pd.DataFrame(
        {
            "ticker": ["A", "B", "C"],
            "exchange": ["US", "US", "US"],
            "coverage_through": ["2026-07-06", "2026-07-06", "2026-07-06"],
            "latest_data_date": ["2026-07-06", "2026-07-06", "2026-07-06"],
            "status": ["ok", "ok", "ok"],
        }
    )
    out = bulk.advance_state(
        state,
        new_max_by_pair={("A", "US"): "2026-07-08"},
        now="2026-07-08T12:00:00Z",
    )
    by = {(r["ticker"], r["exchange"]): r for _, r in out.iterrows()}
    # A got a new bar -> advanced to the actual bar date (not as_of)
    assert by[("A", "US")]["latest_data_date"] == "2026-07-08"
    assert by[("A", "US")]["coverage_through"] == "2026-07-08"
    assert by[("A", "US")]["previous_data_date"] == "2026-07-06"
    assert by[("A", "US")]["status"] == "ok"
    # B and C got no new data -> untouched (coverage never runs past real data)
    assert by[("B", "US")]["coverage_through"] == "2026-07-06"
    assert by[("B", "US")]["latest_data_date"] == "2026-07-06"
    assert by[("C", "US")]["coverage_through"] == "2026-07-06"


def test_advance_state_handles_float_inferred_columns() -> None:
    # read_csv infers all-empty string columns (e.g. "mode") as float64;
    # assigning "bulk" into such a column must not raise on pandas 3.x
    state = pd.DataFrame(
        {
            "ticker": ["A"],
            "exchange": ["US"],
            "coverage_through": ["2026-07-06"],
            "latest_data_date": ["2026-07-06"],
            "status": ["ok"],
            "mode": [float("nan")],
            "fetched_at": [float("nan")],
        }
    )
    out = bulk.advance_state(
        state,
        new_max_by_pair={("A", "US"): "2026-07-08"},
        now="2026-07-08T12:00:00Z",
    )
    row = out.iloc[0]
    assert row["mode"] == "bulk"
    assert row["fetched_at"] == "2026-07-08T12:00:00Z"
    assert row["latest_data_date"] == "2026-07-08"
