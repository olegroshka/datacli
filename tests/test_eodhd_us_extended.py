"""The us_extended lane: universe matching and the fetcher wrappers' wiring."""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_EODHD = _REPO_ROOT / "eodhd"
for path in (_REPO_ROOT, _EODHD):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import eodhd_datasets as reg  # noqa: E402
import fetch_eodhd_us_extended_universe as universe  # noqa: E402

LATEST = date(2026, 9, 15)


def _row(code: str, kind: str = "Common Stock") -> dict[str, str]:
    return {"Code": code, "Name": code.lower(), "Exchange": "NYSE", "Type": kind}


def _wanted(rows: list[tuple[str, date, date]]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=["code", "si_first", "si_last"])


def test_match_prefers_the_list_that_fits_the_symbols_life() -> None:
    active = {c: _row(c) for c in ("LIVE", "REUSED", "MOVED")}
    delisted = {c: _row(c) for c in ("DEAD", "REUSED", "OLDCO_old")}
    wanted = _wanted(
        [
            ("LIVE", date(2018, 1, 12), LATEST),  # still reporting -> active
            ("DEAD", date(2018, 1, 12), date(2020, 3, 13)),  # gone -> delisted
            # gone, and the code now belongs to a newer issuer -> the delisted entry
            ("REUSED", date(2018, 1, 12), date(2021, 6, 15)),
            ("OLDCO", date(2018, 1, 12), date(2019, 6, 14)),  # reused-ticker suffix
            ("MOVED", date(2018, 1, 12), date(2022, 6, 15)),  # gone from listed, still active
            ("GHOST", date(2018, 1, 12), date(2019, 1, 15)),  # nobody knows it
        ]
    )
    frame, unmatched = universe.match_symbols(wanted, active, delisted, latest=LATEST)
    got = {r.si_symbol: (r.Code, r.delisted) for r in frame.itertuples()}
    assert got == {
        "LIVE": ("LIVE", False),
        "DEAD": ("DEAD", True),
        "REUSED": ("REUSED", True),
        "OLDCO": ("OLDCO_old", True),
        "MOVED": ("MOVED", False),
    }
    assert unmatched == ["GHOST"]
    assert list(frame["Code"]) == sorted(frame["Code"])
    assert {"si_first", "si_last", "Type"} <= set(frame.columns)


def test_a_symbol_alive_in_both_lists_takes_the_active_one() -> None:
    frame, _ = universe.match_symbols(
        _wanted([("BOTH", date(2020, 1, 15), date(2026, 8, 31))]),
        {"BOTH": _row("BOTH")},
        {"BOTH": _row("BOTH")},
        latest=LATEST,
    )
    assert frame.loc[0, "delisted"] == False  # noqa: E712  (numpy bool)


def test_lane_is_registered_like_the_etf_lane() -> None:
    lane = reg.LANES["us_extended"]
    assert lane.universe_fetcher == "fetch_eodhd_us_extended_universe.py"
    assert lane.universe_path is not None and lane.universe_path.name == "tickers_US_EXT.parquet"
    assert [d.kind for d in lane.datasets] == ["prices", "splits"]
    assert lane.default_exchange == "US" and lane.universe_code_column == "Code"


def test_wrappers_point_the_shared_fetchers_at_this_lane() -> None:
    import fetch_eodhd_us_etf_prices as etf_prices
    import fetch_eodhd_us_etf_splits as etf_splits
    import fetch_eodhd_us_extended_prices as prices
    import fetch_eodhd_us_extended_splits as splits

    saved = (
        etf_prices.PRICES_PATH,
        etf_prices.PRICES_STATE_PATH,
        etf_prices.ETF_TICKERS_PATH,
        etf_prices.log,
        etf_splits.SPLITS_PATH,
        etf_splits.SPLITS_AUDIT_PATH,
        etf_splits.SPLITS_STATE_PATH,
        etf_splits.ETF_TICKERS_PATH,
        etf_splits.log,
    )
    try:
        prices.configure()
        splits.configure()
        for path in (
            etf_prices.PRICES_PATH,
            etf_prices.PRICES_STATE_PATH,
            etf_prices.ETF_TICKERS_PATH,
            etf_splits.SPLITS_PATH,
            etf_splits.SPLITS_AUDIT_PATH,
            etf_splits.SPLITS_STATE_PATH,
            etf_splits.ETF_TICKERS_PATH,
        ):
            assert path.parent.name == "us_extended"
        assert etf_prices.ETF_TICKERS_PATH == universe.TICKERS_PATH
    finally:
        (
            etf_prices.PRICES_PATH,
            etf_prices.PRICES_STATE_PATH,
            etf_prices.ETF_TICKERS_PATH,
            etf_prices.log,
            etf_splits.SPLITS_PATH,
            etf_splits.SPLITS_AUDIT_PATH,
            etf_splits.SPLITS_STATE_PATH,
            etf_splits.ETF_TICKERS_PATH,
            etf_splits.log,
        ) = saved


def test_routine_targets_skip_delisted_names(tmp_path: Path, monkeypatch) -> None:
    import fetch_eodhd_us_extended_prices as prices

    frame = pd.DataFrame({"Code": ["LIVE", "DEAD"], "delisted": [False, True]})
    frame.to_parquet(tmp_path / "u.parquet")
    monkeypatch.setattr(prices, "TICKERS_PATH", tmp_path / "u.parquet")
    monkeypatch.setattr(sys, "argv", ["x"])
    assert prices.load_target_tickers(explicit_specs=[]) == [("LIVE", "US")]
    monkeypatch.setattr(sys, "argv", ["x", "--include-delisted"])
    assert prices.load_target_tickers(explicit_specs=[]) == [("LIVE", "US"), ("DEAD", "US")]
    assert sys.argv == ["x"]
    assert prices.load_target_tickers(explicit_specs=["AAPL.US"]) == [("AAPL", "US")]
