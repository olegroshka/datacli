"""finra.fails_to_deliver: half-month calendar, publication rule, normalisation, planning, refresh, qc."""

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

from finra import fails_to_deliver as ftd  # noqa: E402
from finra import sec  # noqa: E402
from finra.errors import SecError  # noqa: E402
from finra.store import DayState  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=ET)
H0901, H0816, H0801 = date(2026, 9, 1), date(2026, 8, 16), date(2026, 8, 1)


def _parsed(half: date, rows=None) -> sec.FailsFile:
    lo, _ = sec.half_bounds(half)
    rows = rows or [
        sec.FailRow(lo, "037833100", "AAPL", 1332, "APPLE INC;COM NPV", 324.96),
        sec.FailRow(lo, "084670702", "BRKB", 10, "BERKSHIRE HATHAWAY INC;CL B", None),
    ]
    return sec.FailsFile(
        half, tuple(rows), "x" * 64, len(rows), sum(r.quantity for r in rows)
    )


def test_halves_and_publication_rule() -> None:
    assert ftd.halves(date(2026, 8, 10), date(2026, 9, 20)) == [
        H0801,
        H0816,
        H0901,
        date(2026, 9, 16),
    ]
    assert ftd.halves(date(2026, 8, 16), date(2026, 8, 16)) == [H0816]
    # first half posted at month end, second half about the 15th of the next month, plus 5 days
    assert ftd.published_on(H0901) == date(2026, 10, 5)
    assert ftd.published_on(H0816) == date(2026, 9, 20)
    assert ftd.published_on(date(2026, 12, 16)) == date(2027, 1, 20)
    assert ftd.is_due(H0816, NOW) and not ftd.is_due(H0901, NOW)


def test_frame_from_file() -> None:
    frame = ftd.frame_from_file(_parsed(H0816))
    assert list(frame.columns) == ftd.COLUMNS and len(frame) == 2
    assert frame["quantity"].dtype == "int64" and frame["price"].dtype == "float64"
    assert pd.isna(frame.loc[frame["symbol"] == "BRKB", "price"]).all()
    assert (pd.to_datetime(frame["published_at"]).dt.date == date(2026, 9, 20)).all()
    assert (pd.to_datetime(frame["half_start"]).dt.date == H0816).all()


def test_plan_due_halves_newest_first() -> None:
    plan = ftd.plan_partitions(
        from_date=date(2026, 7, 1), to_date=date(2026, 10, 2), state={}, now=NOW
    )
    assert plan == [
        H0816,
        H0801,
        date(2026, 7, 16),
        date(2026, 7, 1),
    ]  # 09-01 due 10-05: not yet
    state = {
        h.isoformat(): DayState(h.isoformat(), "ok", "sec", sha256="s") for h in plan
    }
    # 08-16 published 09-20 is inside a 35-day window from 10-02; 08-01 (09-04) too; 07-16 (08-20) is not
    plan = ftd.plan_partitions(
        from_date=date(2026, 7, 1),
        to_date=date(2026, 10, 2),
        state=state,
        now=NOW,
        overlap_days=35,
    )
    assert plan == [H0816, H0801]
    assert (
        ftd.plan_partitions(
            from_date=date(2026, 7, 1),
            to_date=date(2026, 10, 2),
            state=state,
            now=NOW,
            overlap_days=0,
        )
        == []
    )


class _Transport:
    name = "sec"

    def __init__(self, script: dict) -> None:
        self.script = script
        self.asked: list = []

    def fetch_partition(self, half_start):
        self.asked.append(half_start)
        outcome = self.script.get(half_start)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_refresh_and_qc(tmp_path: Path) -> None:
    frame = ftd.frame_from_file(_parsed(H0816))
    transport = _Transport(
        {H0816: frame, H0801: None, date(2026, 7, 16): SecError("refused", status=403)}
    )
    report = ftd.refresh(
        run=False,
        root=tmp_path,
        transport=transport,
        from_date=date(2026, 7, 16),
        now=NOW,
    )
    assert (
        report.planned == ("2026-08-16", "2026-08-01", "2026-07-16")
        and transport.asked == []
    )
    report = ftd.refresh(
        run=True,
        root=tmp_path,
        transport=transport,
        from_date=date(2026, 7, 16),
        now=NOW,
    )
    assert report.stored == ("2026-08-16",) and report.absent == ("2026-08-01",)
    assert (
        report.failed == (("2026-07-16", "SecError: refused (HTTP 403)"),)
        and report.rows == 2
    )
    store = ftd.store(tmp_path)
    assert (
        store.days_on_disk() == [H0816]
        and store.day_path(H0816).parent.name == "halves"
    )
    st = store.load_state()["2026-08-16"]
    assert (
        st.rows == 2 and st.short_sum == 1342 and st.total_sum == 1
    )  # quantity sum, settlement days
    qc = ftd.qc(tmp_path, now=NOW)
    checks = {f.check: f for f in qc.findings}
    assert (
        qc.ok and qc.days == 1 and checks["errors"].count == 1 and "gaps" not in checks
    )


def test_qc_flags_violations(tmp_path: Path) -> None:
    store = ftd.store(tmp_path)
    frame = ftd.frame_from_file(_parsed(H0816))
    bad = frame.copy()
    bad.loc[0, "settlement_date"] = date(2026, 9, 1)  # outside the half
    bad["published_at"] = H0816  # not the rule
    store.write_day(H0816, bad)
    qc = ftd.qc(tmp_path, now=NOW)
    checks = {}
    for f in qc.findings:
        checks.setdefault(f.check, []).append(f)
    assert not qc.ok
    assert checks["partition"][0].count == 1 and checks["published_at"][0].count == 2
    assert any("state is missing" in f.message for f in checks["state"])
