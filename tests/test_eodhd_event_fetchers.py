from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import _atomic  # type: ignore  # noqa: E402
import eodhd_event_fetch_common as common  # type: ignore  # noqa: E402
import fetch_eodhd_dividends as dividends  # type: ignore  # noqa: E402
import fetch_eodhd_splits as splits  # type: ignore  # noqa: E402


def test_load_target_tickers_from_coverage(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage_summary.csv"
    pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "both_60q": 1},
            {"ticker": "BBB", "exchange": "PA", "both_60q": 0},
            {"ticker": "CCC", "exchange": "XETRA", "both_60q": 1},
        ]
    ).to_csv(coverage_path, index=False)

    assert common.load_target_tickers([], coverage_path=coverage_path) == [
        ("AAA", "LSE"),
        ("CCC", "XETRA"),
    ]


def test_load_target_tickers_prefers_explicit_specs(tmp_path: Path) -> None:
    coverage_path = tmp_path / "coverage_summary.csv"
    pd.DataFrame([{"ticker": "AAA", "exchange": "LSE", "both_60q": 1}]).to_csv(
        coverage_path, index=False
    )

    assert common.load_target_tickers(
        ["SHEL.LSE", "SAP.XETRA"], coverage_path=coverage_path
    ) == [
        ("SHEL", "LSE"),
        ("SAP", "XETRA"),
    ]


def test_normalize_dividend_rows_maps_fields() -> None:
    df = dividends.normalize_dividend_rows(
        [
            {
                "date": "2025-05-10",
                "declarationDate": "2025-04-01",
                "recordDate": "2025-05-12",
                "paymentDate": "2025-06-01",
                "period": "Quarterly",
                "value": 1.23,
                "unadjustedValue": 1.25,
                "currency": "GBP",
            }
        ],
        "SHEL",
        "LSE",
    )

    assert df.loc[0, "ticker"] == "SHEL"
    assert df.loc[0, "exchange"] == "LSE"
    assert df.loc[0, "ex_date"] == "2025-05-10"
    assert df.loc[0, "dividend"] == 1.23
    assert df.loc[0, "unadjusted_dividend"] == 1.25
    assert df.loc[0, "currency"] == "GBP"


def test_parse_split_factor_parses_ratio() -> None:
    numerator, denominator, ratio = splits.parse_split_factor("4.000000/1.000000")
    assert numerator == 4.0
    assert denominator == 1.0
    assert ratio == 4.0


def test_parse_split_factor_handles_invalid_values() -> None:
    assert splits.parse_split_factor(None) == (None, None, None)
    assert splits.parse_split_factor("bad") == (None, None, None)
    assert splits.parse_split_factor("2/0") == (None, None, None)


def test_normalize_split_rows_maps_fields() -> None:
    df = splits.normalize_split_rows(
        [{"date": "2020-01-01", "split": "2.000000/1.000000"}],
        "SHEL",
        "LSE",
    )

    assert df.loc[0, "ticker"] == "SHEL"
    assert df.loc[0, "exchange"] == "LSE"
    assert df.loc[0, "ex_date"] == "2020-01-01"
    assert df.loc[0, "split_factor"] == "2.000000/1.000000"
    assert df.loc[0, "numerator"] == 2.0
    assert df.loc[0, "denominator"] == 1.0
    assert df.loc[0, "split_ratio"] == 2.0


def test_load_completed_pairs_uses_audit_success_statuses_and_output(
    tmp_path: Path,
) -> None:
    audit_path = tmp_path / "audit.csv"
    output_path = tmp_path / "dividends.parquet"
    pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "status": "ok"},
            {"ticker": "BBB", "exchange": "PA", "status": "empty"},
            {"ticker": "CCC", "exchange": "XETRA", "status": "request_error"},
        ]
    ).to_csv(audit_path, index=False)
    pd.DataFrame(
        [{"ticker": "DDD", "exchange": "SW", "ex_date": "2020-01-01", "dividend": 1.0}]
    ).to_parquet(output_path, index=False)

    completed, audit_df, output_df = common.load_completed_pairs(
        audit_path=audit_path, output_path=output_path
    )

    assert completed == {("AAA", "LSE"), ("BBB", "PA"), ("DDD", "SW")}
    assert audit_df is not None
    assert output_df is not None


def test_build_latest_date_state_returns_latest_event_date_per_pair() -> None:
    existing = pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "LSE", "ex_date": "2026-01-01"},
            {"ticker": "AAA", "exchange": "LSE", "ex_date": "2026-01-03"},
            {"ticker": "BBB", "exchange": "PA", "ex_date": "2025-12-31"},
        ]
    )

    assert common.build_latest_date_state(existing, date_column="ex_date") == {
        ("AAA", "LSE"): "2026-01-03",
        ("BBB", "PA"): "2025-12-31",
    }


def test_choose_incremental_window_uses_coverage_through_for_empty_pairs() -> None:
    assert common.choose_incremental_window(
        last_date=None,
        coverage_through="2026-05-05",
        from_date=None,
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-04-30", "2026-05-06")


def test_choose_incremental_window_uses_coverage_through_when_future_events_exist() -> (
    None
):
    assert common.choose_incremental_window(
        last_date="2027-03-30",
        coverage_through="2026-05-05",
        from_date=None,
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-04-30", "2026-05-06")


def test_choose_incremental_window_future_events_without_coverage_refreshes_tail() -> (
    None
):
    assert common.choose_incremental_window(
        last_date="2027-03-30",
        coverage_through=None,
        from_date=None,
        to_date="2026-05-06",
        overlap_days=5,
        full_refresh=False,
    ) == ("2026-05-01", "2026-05-06")


def test_merge_pair_state_rows_dedupes_latest() -> None:
    existing = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "status": "ok",
                "coverage_through": "2026-05-05",
            }
        ]
    )

    merged = common.merge_pair_state_rows(
        existing,
        [
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "status": "up_to_date",
                "coverage_through": "2026-05-06",
            }
        ],
    )

    assert len(merged) == 1
    assert merged.loc[0, "status"] == "up_to_date"
    assert merged.loc[0, "coverage_through"] == "2026-05-06"


def test_merge_audit_rows_dedupes_latest() -> None:
    existing = pd.DataFrame(
        [{"ticker": "AAA", "exchange": "LSE", "status": "ok", "n_rows": 1}]
    )
    merged = common.merge_audit_rows(
        existing,
        [{"ticker": "AAA", "exchange": "LSE", "status": "empty", "n_rows": 0}],
    )
    assert len(merged) == 1
    assert merged.loc[0, "status"] == "empty"


def test_rebuild_event_audit_preserves_ok_for_pairs_with_existing_history() -> None:
    output = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "ex_date": "2026-01-01",
                "dividend": 1.0,
            },
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "ex_date": "2026-02-01",
                "dividend": 1.1,
            },
        ]
    )
    state = pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "status": "empty",
                "response_rows": 0,
                "latest_data_date": "2026-02-01",
                "coverage_through": "2026-05-06",
                "fetched_at": "2026-05-06T09:00:00+00:00",
            }
        ]
    )

    rebuilt = common.rebuild_event_audit(output=output, state=state)

    assert rebuilt is not None
    assert rebuilt.loc[0, "status"] == "ok"
    assert rebuilt.loc[0, "n_rows"] == 2


def test_rebuild_event_audit_classifies_empty_pairs_from_up_to_date_state() -> None:
    state = pd.DataFrame(
        [
            {
                "ticker": "BBB",
                "exchange": "PA",
                "status": "up_to_date",
                "response_rows": 0,
                "latest_data_date": None,
                "coverage_through": "2026-05-06",
                "fetched_at": "2026-05-06T09:00:00+00:00",
            }
        ]
    )

    rebuilt = common.rebuild_event_audit(output=None, state=state)

    assert rebuilt is not None
    assert rebuilt.loc[0, "status"] == "empty"
    assert rebuilt.loc[0, "n_rows"] == 0


def test_rebuild_event_audit_keeps_existing_rows_without_state() -> None:
    existing_audit = pd.DataFrame(
        [
            {
                "ticker": "CCC",
                "exchange": "XETRA",
                "status": "empty",
                "n_rows": 0,
                "detail": "",
                "fetched_at": "2026-05-06T09:00:00+00:00",
            }
        ]
    )

    rebuilt = common.rebuild_event_audit(
        output=None, state=None, existing_audit=existing_audit
    )

    assert rebuilt is not None
    assert len(rebuilt) == 1
    assert rebuilt.loc[0, "ticker"] == "CCC"
    assert rebuilt.loc[0, "status"] == "empty"


def test_dividends_flush_atomically_replaces_and_reads_fixture_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "dividends_history.parquet"
    pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "LSE",
                "ex_date": "2025-01-01",
                "dividend": 1.0,
            }
        ]
    ).to_parquet(output_path, index=False)

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return [{"date": "2026-01-02", "value": 1.25, "currency": "GBP"}]

    class Session:
        params = {}

        def get(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(dividends, "DIVIDENDS_PATH", output_path)
    monkeypatch.setattr(
        dividends, "DIVIDENDS_AUDIT_PATH", tmp_path / "dividends_fetch_audit.csv"
    )
    monkeypatch.setattr(
        dividends, "DIVIDENDS_STATE_PATH", tmp_path / "dividends_fetch_state.csv"
    )
    monkeypatch.setattr(dividends, "_get_api_key", lambda: "fixture-key")
    monkeypatch.setattr(dividends.requests, "Session", Session)
    monkeypatch.setattr(dividends, "FLUSH_EVERY", 1)
    monkeypatch.setattr(dividends, "DELAY", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "fetch_eodhd_dividends.py",
            "--tickers",
            "BBB.LSE",
            "--from",
            "2026-01-01",
            "--to",
            "2026-01-03",
        ],
    )

    dividends.main()

    result = pd.read_parquet(output_path)
    assert set(zip(result["ticker"], result["ex_date"])) == {
        ("AAA", "2025-01-01"),
        ("BBB", "2026-01-02"),
    }
    assert not output_path.with_name(output_path.name + ".tmp").exists()


def test_atomic_parquet_failure_keeps_previous_output_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output_path = tmp_path / "dividends_history.parquet"
    previous = pd.DataFrame([{"ticker": "AAA", "value": 1.0}])
    previous.to_parquet(output_path, index=False)

    def write_partial_then_fail(self, path, **kwargs):
        Path(path).write_bytes(b"partial parquet")
        raise OSError("fixture parquet failure")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", write_partial_then_fail)
    with pytest.raises(OSError, match="fixture parquet failure"):
        _atomic.to_parquet(pd.DataFrame([{"ticker": "BBB", "value": 2.0}]), output_path)

    assert pd.read_parquet(output_path).equals(previous)
    assert not output_path.with_name(output_path.name + ".tmp").exists()


def test_qualifying_pairs_keep_previously_tracked_pairs(tmp_path: Path) -> None:
    """A firm that stops qualifying (both_60q -> 0) must keep being pulled once
    it is in a lane's fetch state; until 2026-09-12 it silently stopped."""
    coverage = tmp_path / "coverage_summary.csv"
    pd.DataFrame(
        [
            {"ticker": "AAA", "exchange": "US", "both_60q": 1},
            {"ticker": "PLTR", "exchange": "US", "both_60q": 0},
            {"ticker": "ZZZ", "exchange": "US", "both_60q": 0},
        ]
    ).to_csv(coverage, index=False)
    pd.DataFrame(
        [
            {"ticker": "PLTR", "exchange": "US", "status": "ok"},
            {"ticker": "AAA", "exchange": "US", "status": "ok"},
        ]
    ).to_csv(tmp_path / "prices_fetch_state.csv", index=False)

    assert common.qualifying_pairs(coverage) == [("AAA", "US")]
    sticky = common.lane_state_paths(tmp_path)
    assert common.qualifying_pairs(coverage, sticky_from=sticky) == [
        ("AAA", "US"),
        ("PLTR", "US"),  # tracked before, kept; ZZZ never pulled, not added
    ]
    assert common.load_target_tickers(
        [], coverage_path=coverage, sticky_from=sticky
    ) == [("AAA", "US"), ("PLTR", "US")]
    assert common.load_target_tickers(
        ["BBB.LSE"], coverage_path=coverage, sticky_from=sticky
    ) == [("BBB", "LSE")]
    assert common.sticky_pairs([tmp_path / "missing.csv"]) == set()
    with pytest.raises(RuntimeError, match="Coverage file not found"):
        common.qualifying_pairs(tmp_path / "nope.csv")
