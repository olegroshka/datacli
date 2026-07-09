from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_eu_prices as prices  # type: ignore  # noqa: E402


def test_build_resume_state_returns_latest_date_per_pair() -> None:
    existing = pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-01"},
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-03"},
            {"ticker": "BBB", "exchange": "PA", "date": "2025-12-31"},
        ]
    )

    assert prices.build_resume_state(existing) == {
        ("AAA", "LSE"): "2026-01-03",
        ("BBB", "PA"): "2025-12-31",
    }


def test_choose_fetch_window_for_new_ticker_uses_full_requested_range() -> None:
    assert prices.choose_fetch_window(
        last_date=None,
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2005-01-01", "2026-05-06")


def test_choose_fetch_window_for_existing_ticker_uses_overlap_tail() -> None:
    assert prices.choose_fetch_window(
        last_date="2025-12-31",
        coverage_through=None,
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2025-12-26", "2026-05-06")


def test_choose_fetch_window_skips_when_up_to_date() -> None:
    assert (
        prices.choose_fetch_window(
            last_date="2026-05-06",
            coverage_through=None,
            from_date="2005-01-01",
            to_date="2026-05-06",
            overlap_days=5,
            full_refresh=False,
        )
        is None
    )


def test_choose_fetch_window_uses_coverage_through_for_empty_history_pair() -> None:
    assert prices.choose_fetch_window(
        last_date=None,
        coverage_through="2026-05-05",
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-04-30", "2026-05-06")


def test_choose_fetch_window_uses_coverage_through_when_last_date_is_future() -> None:
    assert prices.choose_fetch_window(
        last_date="2027-03-30",
        coverage_through="2026-05-05",
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-04-30", "2026-05-06")


def test_choose_fetch_window_future_last_date_without_coverage_refreshes_tail() -> None:
    assert prices.choose_fetch_window(
        last_date="2027-03-30",
        coverage_through=None,
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-05-01", "2026-05-06")


def test_merge_price_frames_dedupes_overlap_and_keeps_latest() -> None:
    existing = pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-01", "close": 10.0},
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-02", "close": 11.0},
        ]
    )
    new = pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-02", "close": 11.5},
            {"ticker": "AAA", "exchange": "LSE", "date": "2026-01-03", "close": 12.0},
        ]
    )

    merged = prices.merge_price_frames(existing, new)

    assert len(merged) == 3
    assert merged.loc[merged["date"] == "2026-01-02", "close"].iloc[0] == 11.5
    assert list(merged["date"]) == ["2026-01-01", "2026-01-02", "2026-01-03"]
