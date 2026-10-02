"""finra.weekly_flow: calendar, normalisation, planning by publication lag, refresh, qc."""

from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import weekly_flow as wf  # noqa: E402
from finra.errors import EntitlementError  # noqa: E402
from finra.store import DayState  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=ET)  # Thursday
W0824, W0831, W0907 = date(2026, 8, 24), date(2026, 8, 31), date(2026, 9, 7)


def _row(
    week: str,
    tier: str,
    kind: str,
    symbol: str | None,
    mpid: str | None,
    shares: float,
    trades: int,
) -> dict:
    return {
        "weekStartDate": week,
        "tierIdentifier": tier,
        "summaryTypeCode": kind,
        "issueSymbolIdentifier": symbol,
        "issueName": "Name" if symbol else None,
        "MPID": mpid,
        "firmCRDNumber": 123 if mpid else None,
        "marketParticipantName": "Venue" if mpid else None,
        "productTypeCode": "UTP",
        "totalWeeklyTradeCount": trades,
        "totalWeeklyShareQuantity": shares,
        "totalNotionalSum": shares * 10.0,
        "initialPublishedDate": "2026-09-28",
        "lastUpdateDate": "2026-09-28",
        "lastReportedDate": "2026-08-28",
    }


ROWS = [
    _row("2026-08-24", "T2", "ATS_W_SMBL_FIRM", "GRBK", "MSPL", 68900, 309),
    _row("2026-08-24", "T2", "ATS_W_SMBL", "GRBK", None, 120000.5, 600),
    _row("2026-08-24", "T2", "OTC_W_SMBL", "GRBK", None, 50000, 200),
    _row("2026-08-24", "T2", "ATS_W_FIRM", None, "MSPL", 9e9, 1000000),
]


# --------------------------------------------------------------------------- #
# calendar
# --------------------------------------------------------------------------- #
def test_week_calendar_and_publication_lag() -> None:
    assert wf.week_monday(date(2026, 10, 1)) == date(2026, 9, 28)
    assert wf.weeks(date(2026, 8, 26), date(2026, 9, 8)) == [W0824, W0831, W0907]
    assert wf.first_publication(W0907, "T1") == date(2026, 9, 28)  # verified live
    assert wf.first_publication(W0824, "T2") == date(2026, 9, 28)  # verified live
    assert wf.is_due(W0907, "T1", NOW) and not wf.is_due(W0907, "T2", NOW)
    assert wf.is_due(W0824, "T2", NOW) and not wf.is_due(W0831, "T2", NOW)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def test_frame_from_api_rows_keeps_every_row_kind_and_types() -> None:
    frame = wf.frame_from_api_rows(W0824, "T2", ROWS)
    assert list(frame.columns) == wf.COLUMNS and len(frame) == 4
    assert (
        frame["share_quantity"].dtype == "float64"
        and frame["trade_count"].dtype == "int64"
    )
    assert str(frame["published_at"].iloc[0]) == "2026-09-28"
    firm = frame[frame["summary_type"] == "ATS_W_FIRM"].iloc[0]
    assert (
        pd.isna(firm["symbol"]) and firm["mpid"] == "MSPL" and firm["firm_crd"] == 123
    )
    sym = frame[frame["summary_type"] == "ATS_W_SMBL"].iloc[0]
    assert (
        sym["share_quantity"] == 120000.5
        and pd.isna(sym["mpid"])
        and pd.isna(sym["firm_crd"])
    )
    assert (frame["source"] == "api").all()
    assert wf.frame_from_api_rows(W0824, "T2", []).empty


def test_symbol_that_changed_tape_mid_week_keeps_both_product_rows() -> None:
    cts = dict(
        _row("2026-06-08", "T1", "ATS_W_SMBL", "FITB", None, 1387778, 17719),
        productTypeCode="CTS",
    )
    utp = dict(
        _row("2026-06-08", "T1", "ATS_W_SMBL", "FITB", None, 6132267, 86698),
        productTypeCode="UTP",
    )
    frame = wf.frame_from_api_rows(date(2026, 6, 8), "T1", [cts, utp])
    assert len(frame) == 2 and sorted(frame["product_type"]) == ["CTS", "UTP"]


def test_ticker_reassigned_to_another_issuer_keeps_both_issues() -> None:
    old_issuer = dict(
        _row("2026-02-23", "T2", "ATS_W_SMBL", "RNA", None, 8553702, 40249),
        issueName="Avidity Biosciences, Inc.",
    )
    new_issuer = dict(
        _row("2026-02-23", "T2", "ATS_W_SMBL", "RNA", None, 907252, 8774),
        issueName="Atrium Therapeutics, Inc.",
    )
    frame = wf.frame_from_api_rows(date(2026, 2, 23), "T2", [old_issuer, new_issuer])
    assert len(frame) == 2 and set(frame["issue_name"]) == {
        "Avidity Biosciences, Inc.",
        "Atrium Therapeutics, Inc.",
    }


def test_frame_rejects_foreign_partitions_duplicates_and_negatives() -> None:
    with pytest.raises(ValueError, match="expected 2026-08-24/T1"):
        wf.frame_from_api_rows(W0824, "T1", ROWS)
    with pytest.raises(ValueError, match="duplicate"):
        wf.frame_from_api_rows(W0824, "T2", ROWS + [ROWS[1]])
    bad = dict(ROWS[1], totalWeeklyShareQuantity=-1)
    with pytest.raises(ValueError, match="negative"):
        wf.frame_from_api_rows(W0824, "T2", [bad])
    with pytest.raises(ValueError, match="lack fields"):
        wf.frame_from_api_rows(W0824, "T2", [{"weekStartDate": "2026-08-24"}])


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _state(**by_key: str) -> dict[str, DayState]:
    out = {}
    for key, status in by_key.items():
        week, tier = key.split("/")
        st = DayState(week, status, "api", sha256="s", part=tier)
        out[st.key] = st
    return out


def test_plan_only_due_partitions_newest_first() -> None:
    plan = wf.plan_partitions(
        from_date=date(2026, 8, 17), to_date=date(2026, 10, 1), state={}, now=NOW
    )
    # T1 due through 09-07, T2 due through 08-24; weeks 08-17 .. 10-01
    assert plan == [
        (W0907, "T1"),
        (W0831, "T1"),
        (W0824, "T2"),
        (W0824, "T1"),
        (date(2026, 8, 17), "T2"),
        (date(2026, 8, 17), "T1"),
    ]


def test_plan_skips_ok_outside_the_overlap_and_rechecks_inside() -> None:
    state = _state(
        **{
            "2026-08-17/T1": "ok",
            "2026-08-24/T1": "ok",
            "2026-08-31/T1": "ok",
            "2026-09-07/T1": "ok",
            "2026-08-17/T2": "ok",
            "2026-08-24/T2": "absent",
        }
    )
    # latest due T1 week is 09-07; a 14-day overlap re-checks 08-24 .. 09-07; absent T2 08-24 is inside its own window
    plan = wf.plan_partitions(
        from_date=date(2026, 8, 17),
        to_date=date(2026, 10, 1),
        state=state,
        now=NOW,
        overlap_days=14,
    )
    assert plan == [
        (W0907, "T1"),
        (W0831, "T1"),
        (W0824, "T2"),
        (W0824, "T1"),
        (
            date(2026, 8, 17),
            "T2",
        ),  # inside T2's own window: latest due T2 week is 08-24
    ]
    plan = wf.plan_partitions(
        from_date=date(2026, 8, 17),
        to_date=date(2026, 10, 1),
        state=state,
        now=NOW,
        overlap_days=0,
    )
    assert plan == []
    state["2026-08-17/T2"] = DayState("2026-08-17", "error", "api", part="T2")
    plan = wf.plan_partitions(
        from_date=date(2026, 8, 17),
        to_date=date(2026, 10, 1),
        state=state,
        now=NOW,
        overlap_days=0,
    )
    assert plan == [(date(2026, 8, 17), "T2")]


# --------------------------------------------------------------------------- #
# refresh + qc with a scripted transport
# --------------------------------------------------------------------------- #
class _Transport:
    name = "api"

    def __init__(self, script: dict) -> None:
        self.script = script
        self.asked: list = []

    def fetch_partition(self, week_start, tier):
        self.asked.append((week_start, tier))
        outcome = self.script.get((week_start, tier))
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_refresh_dry_run_and_run(tmp_path: Path) -> None:
    transport = _Transport({})
    report = wf.refresh(
        run=False, root=tmp_path, transport=transport, from_date=W0824, now=NOW
    )
    assert report.planned == (
        "2026-09-07/T1",
        "2026-08-31/T1",
        "2026-08-24/T2",
        "2026-08-24/T1",
    )
    assert transport.asked == [] and not (tmp_path / "weekly_flow").exists()

    frame = wf.frame_from_api_rows(W0824, "T2", ROWS)
    transport = _Transport(
        {(W0824, "T2"): frame, (W0907, "T1"): EntitlementError("no", status=403)}
    )
    report = wf.refresh(
        run=True,
        root=tmp_path,
        transport=transport,
        from_date=W0824,
        now=NOW,
        limit_days=3,
    )
    assert report.stored == ("2026-08-24/T2",) and report.absent == ("2026-08-31/T1",)
    assert report.failed == (("2026-09-07/T1", "EntitlementError: no (HTTP 403)"),)
    assert report.skipped == ("2026-08-24/T1",) and report.rows == 4 and not report.ok
    store = wf.store(tmp_path)
    assert store.partitions_on_disk() == [(W0824, "T2")]
    assert store.day_path(W0824, "T2").name == "2026-08-24_T2.parquet"
    state = store.load_state()
    assert state["2026-08-24/T2"].status == "ok" and state["2026-08-24/T2"].rows == 4
    assert state["2026-08-24/T2"].short_sum == 1001109  # trade counts
    assert (
        state["2026-08-31/T1"].status == "absent"
        and state["2026-09-07/T1"].status == "error"
    )

    # restatement inside the overlap: one share quantity changes
    changed = wf.frame_from_api_rows(
        W0824, "T2", [dict(ROWS[0], totalWeeklyShareQuantity=70000), *ROWS[1:]]
    )
    report = wf.refresh(
        run=True,
        root=tmp_path,
        transport=_Transport({(W0824, "T2"): changed}),
        from_date=W0824,
        to_date=W0824,
        now=NOW,
    )
    assert report.restated == ("2026-08-24/T2",)
    assert store.load_state()["2026-08-24/T2"].detail.startswith("restated")

    qc = wf.qc(tmp_path, now=NOW)
    checks = {f.check: f for f in qc.findings}
    assert qc.ok and qc.days == 1
    assert checks["errors"].count == 1  # the 09-07/T1 error is reported
    assert "gaps" not in checks  # 08-24/T1 became absent in the second run, so no gap


def test_qc_reports_gaps_and_foreign_rows(tmp_path: Path) -> None:
    store = wf.store(tmp_path)
    frame = wf.frame_from_api_rows(W0824, "T2", ROWS)
    store.write_day(W0824, frame, part="T2")
    store.upsert_state([DayState("2026-08-24", "ok", "api", rows=4, part="T2")])
    wrong = frame.copy()
    wrong["tier"] = "T2"
    store.write_day(W0824, wrong, part="T1")  # rows say T2 inside the T1 file
    qc = wf.qc(tmp_path, now=NOW)
    checks = {f.check: f for f in qc.findings}
    assert not qc.ok and checks["partition"].count == 4
    assert "file on disk but state is missing" in checks["state"].message
