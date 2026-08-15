from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import eodhd_datasets as reg  # type: ignore  # noqa: E402
import status_eodhd as st  # type: ignore  # noqa: E402


def _price_state() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "status": "ok",
                "coverage_through": "2026-06-07",
                "latest_data_date": "2026-06-05",
                "fetched_at": "2026-06-07T17:19:00+00:00",
            },
            {
                "ticker": "BBB",
                "exchange": "US",
                "status": "up_to_date",
                "coverage_through": "2026-06-07",
                "latest_data_date": "2026-06-04",
                "fetched_at": "2026-06-07T17:20:00+00:00",
            },
        ]
    )


def _prices_parquet() -> pd.DataFrame:
    dates = pd.to_datetime(["2026-06-04", "2026-06-05"])
    return pd.DataFrame(
        {
            "date": list(dates) + list(dates),
            "ticker": ["AAA", "AAA", "BBB", "BBB"],
            "exchange": ["US", "US", "US", "US"],
            "close": [1.0, 2.0, 3.0, 4.0],
        }
    )


def _make_lane(root: Path, datasets: list[reg.DatasetSpec]) -> reg.LaneConfig:
    return reg.LaneConfig(
        name="test_lane",
        region="US",
        asset_class="common",
        datasets=tuple(datasets),
        root=root,
    )


def test_staleness_days_clamps_and_handles_missing() -> None:
    as_of = pd.Timestamp("2026-07-06")
    assert st.staleness_days(pd.Timestamp("2026-07-01"), as_of) == 5
    # A future announced date must not read as negative age.
    assert st.staleness_days(pd.Timestamp("2027-03-30"), as_of) == 0
    assert st.staleness_days(None, as_of) is None


def test_prices_record_uses_state_and_flags_fresh(tmp_path: Path) -> None:
    _price_state().to_csv(tmp_path / "prices_fetch_state.csv", index=False)
    _prices_parquet().to_parquet(tmp_path / "prices_daily.parquet", index=False)
    lane = _make_lane(tmp_path, [reg.prices_spec()])

    rec = st.collect_dataset(
        lane,
        lane.datasets[0],
        as_of_ts=pd.Timestamp("2026-06-08"),
        stale_days=7,
        deep=False,
    )

    assert rec["source"] == "state"
    assert rec["rows"] == 4
    assert rec["pairs"] == 2
    assert rec["last_data"] == "2026-06-05"  # max latest_data_date
    assert rec["coverage"] == "2026-06-07"
    assert rec["freshness"] == "2026-06-05"  # prices anchor = last real bar
    assert rec["stale"] is False
    assert rec["status"] == {"ok": 1, "up_to_date": 1}


def test_event_freshness_uses_coverage_not_future_ex_date(tmp_path: Path) -> None:
    # A future announced ex-date must not make the dataset look "fresh"; coverage
    # (how far we queried) is the correct staleness anchor for event lanes.
    pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "status": "ok",
                "coverage_through": "2026-05-06",
                "latest_data_date": "2027-03-30",  # future announced dividend
                "fetched_at": "2026-05-06T09:00:00+00:00",
            }
        ]
    ).to_csv(tmp_path / "dividends_fetch_state.csv", index=False)
    pd.DataFrame(
        {
            "ticker": ["AAA"],
            "exchange": ["US"],
            "ex_date": pd.to_datetime(["2027-03-30"]),
        }
    ).to_parquet(tmp_path / "dividends_history.parquet", index=False)
    lane = _make_lane(tmp_path, [reg.event_spec("dividends")])

    rec = st.collect_dataset(
        lane,
        lane.datasets[0],
        as_of_ts=pd.Timestamp("2026-07-06"),
        stale_days=7,
        deep=False,
    )

    assert rec["freshness"] == "2026-05-06"  # coverage, not the 2027 ex-date
    assert rec["stale"] is True
    assert rec["stale_days"] == 61


def test_snapshot_without_state_falls_back(tmp_path: Path) -> None:
    pd.DataFrame(
        {
            "ticker": ["AAA", "BBB"],
            "exchange": ["US", "US"],
            "filing_date": pd.to_datetime(["2026-05-05", "2026-05-01"]),
            "date": pd.to_datetime(["2026-03-31", "2026-03-31"]),
        }
    ).to_parquet(tmp_path / "fundamentals_quarterly.parquet", index=False)
    lane = _make_lane(tmp_path, [reg.fundamentals_spec()])

    rec = st.collect_dataset(
        lane,
        lane.datasets[0],
        as_of_ts=pd.Timestamp("2026-07-06"),
        stale_days=7,
        deep=True,
    )

    assert rec["source"] == "snapshot"
    assert rec["status"] == "snapshot"
    assert rec["last_data"] == "2026-05-05"  # max filing_date
    assert rec["freshness"] == "2026-05-05"
    assert rec["pairs"] == 2  # deep -> distinct key pairs read from parquet
    assert rec["stale"] is True


def test_absent_dataset(tmp_path: Path) -> None:
    lane = _make_lane(tmp_path, [reg.prices_spec()])
    rec = st.collect_dataset(
        lane,
        lane.datasets[0],
        as_of_ts=pd.Timestamp("2026-07-06"),
        stale_days=7,
        deep=False,
    )
    assert rec["source"] == "absent"
    assert rec["rows"] is None
    assert rec["stale"] is None


def test_discovery_flags_unregistered_dir(tmp_path: Path) -> None:
    (tmp_path / "us_common").mkdir()
    (tmp_path / "mystery_lane").mkdir()
    (tmp_path / "probe_cache").mkdir()
    (tmp_path / ".sync").mkdir()
    found = reg.discover_lane_dirs(tmp_path)
    assert "mystery_lane" in found
    assert ".sync" not in found
    unregistered = [
        name
        for name in found
        if name not in reg.registered_lane_names() and name not in reg.NON_LANE_DIRS
    ]
    assert unregistered == ["mystery_lane"]


def test_registry_covers_all_six_lanes() -> None:
    assert set(reg.LANES) == {
        "us_common",
        "uk_eu",
        "us_etf",
        "index_ref",
        "uk_eu_etf",
        "uk_eu_index_ref",
    }
    # us_common carries prices, both event lanes, and a fundamentals snapshot.
    kinds = {d.kind for d in reg.LANES["us_common"].datasets}
    assert kinds == {"prices", "dividends", "splits", "fundamentals"}
