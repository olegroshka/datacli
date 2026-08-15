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


def test_registry_covers_all_lanes() -> None:
    assert set(reg.LANES) == {
        "us_common",
        "uk_eu",
        "us_etf",
        "index_ref",
        "uk_eu_etf",
        "uk_eu_index_ref",
        "news",
    }
    # us_common carries prices, both event lanes, and a fundamentals snapshot.
    kinds = {d.kind for d in reg.LANES["us_common"].datasets}
    assert kinds == {"prices", "dividends", "splits", "fundamentals"}


def test_news_lane_is_day_keyed_and_partitioned(tmp_path: Path) -> None:
    news_ds = next(d for d in reg.LANES["news"].datasets if d.kind == "news")
    assert news_ds.kind == "news"
    assert news_ds.partitioned is True
    assert news_ds.key_cols == ("date",)
    assert news_ds.ticker_keyed is False
    assert news_ds.output_glob(tmp_path) == str(tmp_path / "articles" / "*.parquet")
    # ticker-keyed datasets keep the plain path
    (prices_ds,) = [d for d in reg.LANES["us_common"].datasets if d.kind == "prices"]
    assert prices_ds.ticker_keyed is True
    assert prices_ds.output_glob(tmp_path) == str(tmp_path / "prices_daily.parquet")


def test_status_reads_partitioned_output(tmp_path: Path) -> None:
    """A partition directory is counted across all its files; empty dir = absent."""
    lane_root = tmp_path / "news"
    part_dir = lane_root / "articles"
    part_dir.mkdir(parents=True)
    assert st.parquet_num_rows(part_dir) is None
    assert st.parquet_columns(part_dir) == set()
    for month, n in (("2026-07", 3), ("2026-08", 2)):
        pd.DataFrame(
            {
                "article_id": [f"{month}-{i}" for i in range(n)],
                "date": ["2026-07-01"] * n,
            }
        ).to_parquet(part_dir / f"{month}.parquet", index=False)
    assert st.parquet_num_rows(part_dir) == 5
    assert st.parquet_columns(part_dir) == {"article_id", "date"}
    assert st.parquet_max_date(part_dir, "date") == pd.Timestamp("2026-07-01")

    news_ds = next(d for d in reg.LANES["news"].datasets if d.kind == "news")
    lane = reg.LaneConfig(
        name="news",
        region="Global",
        asset_class="news",
        datasets=(news_ds,),
        root=lane_root,
    )
    pd.DataFrame(
        {
            "date": ["2026-08-13", "2026-08-14"],
            "status": ["ok", "ok"],
            "articles": [2837, 2900],
            "fetched_at": ["2026-08-15T10:00:00+00:00"] * 2,
        }
    ).to_csv(lane_root / "news_fetch_state.csv", index=False)
    rec = st.collect_dataset(
        lane, news_ds, as_of_ts=pd.Timestamp("2026-08-15"), stale_days=7, deep=True
    )
    assert rec["rows"] == 5
    assert rec["pairs"] == 2  # distinct crawled days
    assert rec["freshness"] == "2026-08-14"
    assert rec["stale"] is False
    assert rec["output_max"] == "2026-07-01"


def _absent_record(lane: str = "us_common", dataset: str = "prices") -> dict:
    return {
        "lane": lane,
        "dataset": dataset,
        "kind": dataset,
        "source": "absent",
        "rows": None,
        "pairs": None,
        "last_data": None,
        "coverage": None,
        "fetched": None,
        "freshness": None,
        "stale_days": None,
        "stale": None,
        "status": "absent",
    }


def test_first_fill_hint_only_when_everything_is_absent(tmp_path: Path) -> None:
    root = tmp_path / "eodhd"
    absent = [_absent_record(), _absent_record("uk_eu", "dividends")]
    hint = st.first_fill_hint(absent, root)
    assert hint is not None
    assert str(root) in hint
    assert "First fill" in hint and "--with-fundamentals" in hint
    # one populated dataset -> the table already tells the per-lane story
    partial = [_absent_record(), {**_absent_record("uk_eu"), "source": "state"}]
    assert st.first_fill_hint(partial, root) is None
    assert st.first_fill_hint([], root) is None


def test_render_markdown_carries_first_fill_note(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(st, "discovery_warnings", lambda: [])
    md = st.render_markdown([_absent_record()], as_of="2026-08-15", stale_days=7)
    assert "**Note:** no data under" in md
    assert "news = last crawled UTC day" in md
    populated = {**_absent_record(), "source": "state", "rows": 3}
    md = st.render_markdown([populated], as_of="2026-08-15", stale_days=7)
    assert "**Note:** no data under" not in md


def test_print_status_prints_hint_on_empty_root(capsys: pytest.CaptureFixture) -> None:
    import _render  # type: ignore

    console = _render.make_console(no_color=True)
    st.print_status(console, [_absent_record()], as_of="2026-08-15", stale_days=7)
    out = capsys.readouterr().out
    assert "1 absent" in out
    assert "first fill" in out


def test_parser_prog_from_env(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("DATACLI_PROG", "eodhd status")
    with pytest.raises(SystemExit) as exc:
        st.parse_args(["--help"])
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())  # undo argparse wrapping
    assert out.startswith("usage: eodhd status")
    assert "--lane" in out and "resolved data root" in out
    assert "data/raw/eodhd/" not in out
    # without the env var argparse falls back to the script name
    monkeypatch.delenv("DATACLI_PROG", raising=False)
    with pytest.raises(SystemExit):
        st.parse_args(["--help"])
    assert "usage: eodhd status" not in capsys.readouterr().out


def test_pairs_behind_and_catch_up_hints() -> None:
    state = pd.DataFrame(
        {
            "ticker": ["A", "B", "C", "D"],
            "exchange": ["US"] * 4,
            "latest_data_date": ["2026-08-14", "2026-08-07", "2026-08-01", None],
        }
    )
    as_of = pd.Timestamp("2026-08-15")
    # cutoff = as_of - 7d = 2026-08-08: B (08-07) and C (08-01) are behind; D unknown
    assert st.pairs_behind(state, "latest_data_date", as_of_ts=as_of, days=7) == 2
    assert st.pairs_behind(state, "missing_col", as_of_ts=as_of, days=7) is None
    records = [
        {
            "lane": "us_common",
            "dataset": "prices",
            "kind": "prices",
            "pairs": 4,
            "pairs_behind": 2,
        },
        {
            "lane": "us_common",
            "dataset": "dividends",
            "kind": "dividends",
            "pairs": 4,
            "pairs_behind": 0,
        },
        {
            "lane": "us_common",
            "dataset": "fundamentals_q",
            "kind": "fundamentals",
            "pairs": 4,
            "pairs_behind": 3,
        },
        {
            "lane": "news",
            "dataset": "news_articles",
            "kind": "news",
            "pairs": 4,
            "pairs_behind": None,
        },
    ]
    hints = st.catch_up_hints(records, stale_days=7)
    assert len(hints) == 1 and hints[0].startswith(
        "us_common/prices: 2 of 4 pairs are > 7d behind"
    )
    assert "refresh us_common --run" in hints[0] and "--fast --days N" in hints[0]
