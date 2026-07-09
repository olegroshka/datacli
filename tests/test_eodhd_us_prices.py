from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fetch_eodhd_us_prices as prices  # type: ignore  # noqa: E402


def test_load_target_tickers_uses_us_common_coverage(
    tmp_path: Path, monkeypatch
) -> None:
    coverage_path = tmp_path / "coverage_summary.csv"
    pd.DataFrame(
        [
            {"ticker": "AAPL", "exchange": "US", "both_60q": 1},
            {"ticker": "SPY", "exchange": "US", "both_60q": 0},
            {"ticker": "MSFT", "exchange": "US", "both_60q": 1},
        ]
    ).to_csv(coverage_path, index=False)
    monkeypatch.setattr(prices, "COVERAGE_PATH", coverage_path)

    assert prices.load_target_tickers(explicit_specs=[], limit=0) == [
        ("AAPL", "US"),
        ("MSFT", "US"),
    ]


def test_load_target_tickers_prefers_explicit_specs() -> None:
    assert prices.load_target_tickers(
        explicit_specs=["AAPL.US", "MSFT.US"], limit=0
    ) == [
        ("AAPL", "US"),
        ("MSFT", "US"),
    ]


def test_build_resume_state_returns_latest_date_per_pair() -> None:
    existing = pd.DataFrame(
        [
            {"ticker": "AAPL", "exchange": "US", "date": "2026-01-01"},
            {"ticker": "AAPL", "exchange": "US", "date": "2026-01-03"},
            {"ticker": "MSFT", "exchange": "US", "date": "2025-12-31"},
        ]
    )

    assert prices.build_resume_state(existing) == {
        ("AAPL", "US"): "2026-01-03",
        ("MSFT", "US"): "2025-12-31",
    }


def test_choose_fetch_window_existing_ticker_uses_overlap_tail() -> None:
    assert prices.choose_fetch_window(
        last_date="2025-12-31",
        coverage_through=None,
        from_date="2005-01-01",
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2025-12-26", "2026-05-06")
