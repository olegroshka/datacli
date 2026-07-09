from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import fundamentals_refresh_common as fr  # type: ignore  # noqa: E402

AS_OF = pd.Timestamp("2026-07-07T00:00:00")


def _panel() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ticker": "AAA",
                "exchange": "US",
                "statement": "bs",
                "date": "2026-03-31",
                "filing_date": "2026-05-01",
            },
            {
                "ticker": "AAA",
                "exchange": "US",
                "statement": "cf",
                "date": "2026-03-31",
                "filing_date": "2026-05-01",
            },
            {
                "ticker": "AAA",
                "exchange": "US",
                "statement": "bs",
                "date": "2025-12-31",
                "filing_date": "2026-02-01",
            },
            {
                "ticker": "BBB",
                "exchange": "US",
                "statement": "bs",
                "date": "2026-06-30",
                "filing_date": "2026-07-05",
            },
        ]
    )


# --------------------------------------------------------------------------- #
# parsing
# --------------------------------------------------------------------------- #
def test_parse_calendar_code() -> None:
    assert fr.parse_calendar_code("AAPL.US") == ("AAPL", "US")
    assert fr.parse_calendar_code("vod.lse") == ("VOD", "LSE")
    assert fr.parse_calendar_code("NODOT") is None
    assert fr.parse_calendar_code("") is None


# --------------------------------------------------------------------------- #
# target selection
# --------------------------------------------------------------------------- #
def test_select_targets_full_and_backfill() -> None:
    cands = [("A", "US"), ("B", "US"), ("C", "US")]
    present = [("A", "US"), ("B", "US")]
    assert fr.select_targets(cands, mode=fr.MODE_FULL, present=present) == cands
    assert fr.select_targets(cands, mode=fr.MODE_BACKFILL, present=present) == [
        ("C", "US")
    ]


def test_select_targets_update_with_reported() -> None:
    cands = [("A", "US"), ("B", "US"), ("C", "US"), ("D", "US")]
    present = [("A", "US"), ("B", "US"), ("C", "US")]  # D is new
    reported = [("B", "US")]  # only B reported
    targets = fr.select_targets(
        cands, mode=fr.MODE_UPDATE, present=present, reported=reported, as_of=AS_OF
    )
    assert targets == [("B", "US"), ("D", "US")]  # reported present + new, in order


def test_select_targets_update_with_stale_days() -> None:
    cands = [("A", "US"), ("B", "US"), ("C", "US")]
    present = cands
    state = {
        ("A", "US"): {"fetched_at": pd.Timestamp("2026-07-06")},  # fresh
        ("B", "US"): {"fetched_at": pd.Timestamp("2026-05-01")},  # stale
        # C has no state row -> stale
    }
    targets = fr.select_targets(
        cands,
        mode=fr.MODE_UPDATE,
        present=present,
        state=state,
        stale_days=30,
        as_of=AS_OF,
    )
    assert targets == [("B", "US"), ("C", "US")]


def test_is_stale_variants() -> None:
    assert fr.is_stale(("X", "US"), None, as_of=AS_OF, stale_days=30) is True
    assert fr.is_stale(("X", "US"), {}, as_of=AS_OF, stale_days=30) is True
    state = {("X", "US"): {"fetched_at": None}}
    assert fr.is_stale(("X", "US"), state, as_of=AS_OF, stale_days=30) is True
    fresh = {("X", "US"): {"fetched_at": pd.Timestamp("2026-07-06")}}
    assert fr.is_stale(("X", "US"), fresh, as_of=AS_OF, stale_days=30) is False


# --------------------------------------------------------------------------- #
# panel summary + sidecar
# --------------------------------------------------------------------------- #
def test_summarize_panel() -> None:
    summ = fr.summarize_panel(_panel())
    assert summ[("AAA", "US")]["latest_filing_date"] == "2026-05-01"
    assert summ[("AAA", "US")]["latest_statement_date"] == "2026-03-31"
    assert summ[("AAA", "US")]["n_quarters"] == 2  # 2026-03-31 and 2025-12-31
    assert summ[("BBB", "US")]["latest_statement_date"] == "2026-06-30"
    assert summ[("BBB", "US")]["n_quarters"] == 1


def test_build_state_rows_stamps_refreshed_keeps_prior() -> None:
    t0 = pd.Timestamp("2026-05-06T00:00:00")
    t1 = pd.Timestamp("2026-07-07T12:00:00")
    prior = {("AAA", "US"): {"fetched_at": t0, "status": "ok"}}
    rows = fr.build_state_rows(_panel(), prior=prior, refreshed=[("BBB", "US")], now=t1)
    by_firm = {(r["ticker"], r["exchange"]): r for r in rows}
    # AAA present in panel but not refreshed -> keep prior fetched_at
    assert by_firm[("AAA", "US")]["fetched_at"].startswith("2026-05-06")
    # BBB refreshed this run -> stamped now
    assert by_firm[("BBB", "US")]["fetched_at"].startswith("2026-07-07")
    assert by_firm[("BBB", "US")]["status"] == "ok"


def test_write_then_load_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / fr.STATE_FILENAME
    t1 = pd.Timestamp("2026-07-07T10:00:00")
    fr.write_state(path, _panel(), refreshed=[("AAA", "US"), ("BBB", "US")], now=t1)
    loaded = fr.load_state(path)
    assert set(loaded) == {("AAA", "US"), ("BBB", "US")}
    assert loaded[("AAA", "US")]["fetched_at"].date().isoformat() == "2026-07-07"

    # Second run refreshes only AAA at a later time; BBB keeps its prior stamp.
    t2 = pd.Timestamp("2026-07-14T10:00:00")
    fr.write_state(path, _panel(), refreshed=[("AAA", "US")], now=t2)
    loaded2 = fr.load_state(path)
    assert loaded2[("AAA", "US")]["fetched_at"].date().isoformat() == "2026-07-14"
    assert loaded2[("BBB", "US")]["fetched_at"].date().isoformat() == "2026-07-07"


def test_resolve_reported_from() -> None:
    as_of = pd.Timestamp("2026-07-07")
    # explicit override wins
    assert fr.resolve_reported_from("2026-01-01", _panel(), as_of=as_of) == "2026-01-01"
    # else the panel's latest filing_date
    assert fr.resolve_reported_from(None, _panel(), as_of=as_of) == "2026-07-05"
    # else a lookback when no panel
    assert (
        fr.resolve_reported_from(
            None, pd.DataFrame(), as_of=as_of, default_lookback_days=60
        )
        == "2026-05-08"
    )


def test_empty_firm_recorded(tmp_path: Path) -> None:
    path = tmp_path / fr.STATE_FILENAME
    t1 = pd.Timestamp("2026-07-07T10:00:00")
    fr.write_state(
        path, _panel(), refreshed=[("AAA", "US")], empty=[("ZZZ", "US")], now=t1
    )
    loaded = fr.load_state(path)
    assert loaded[("ZZZ", "US")]["status"] == "empty"
