"""finra.short_interest: settlement calendar, publication rule, normalisation, planning, refresh, qc."""

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

from finra import short_interest as si  # noqa: E402
from finra.errors import EntitlementError  # noqa: E402
from finra.store import DayState  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(2026, 10, 2, 12, 0, tzinfo=ET)

# the first and last settlement dates FINRA's partitions endpoint listed on 2026-10-02
LIVE_PARTITIONS = [
    "2017-12-29",
    "2018-01-12",
    "2018-01-31",
    "2026-07-31",
    "2026-08-14",
    "2026-08-31",
    "2026-09-15",
]


def _row(
    settlement: str,
    symbol: str,
    position: int = 1000,
    prev: int = 900,
    adv: int = 100,
    **extra,
) -> dict:
    base = {
        "settlementDate": settlement,
        "symbolCode": symbol,
        "issueName": f"{symbol} Inc.",
        "marketClassCode": "NYSE",
        "issuerServicesGroupExchangeCode": "A",
        "currentShortPositionQuantity": position,
        "previousShortPositionQuantity": prev,
        "changePreviousNumber": position - prev,
        "changePercent": round((position - prev) / prev * 100, 2),
        "averageDailyVolumeQuantity": adv,
        "daysToCoverQuantity": round(position / adv, 2),
        "revisionFlag": None,
        "stockSplitFlag": None,
        "accountingYearMonthNumber": int(settlement.replace("-", "")),
    }
    base.update(extra)
    return base


# --------------------------------------------------------------------------- #
# calendar + publication rule
# --------------------------------------------------------------------------- #
def test_settlement_dates_match_the_live_partition_list() -> None:
    dates = {
        d.isoformat() for d in si.settlement_dates(date(2017, 12, 1), date(2026, 9, 30))
    }
    for live in LIVE_PARTITIONS:
        assert live in dates, live
    # mid-month moves back when the 15th is a weekend or holiday; month-end to the last trading day
    assert si.settlement_dates(date(2018, 1, 1), date(2018, 1, 31)) == [
        date(2018, 1, 12),
        date(2018, 1, 31),
    ]  # MLK 15th
    assert si.settlement_dates(date(2026, 8, 1), date(2026, 8, 31)) == [
        date(2026, 8, 14),
        date(2026, 8, 31),
    ]  # Sat 15th
    assert si.settlement_dates(date(2026, 9, 16), date(2026, 9, 30)) == [
        date(2026, 9, 30)
    ]
    assert (
        len(si.settlement_dates(date(2017, 12, 29), date(2026, 9, 15))) == 210
    )  # what FINRA listed


def test_published_on_is_seven_business_days_later() -> None:
    assert si.published_on(date(2025, 12, 31)) == date(2026, 1, 12)
    assert si.published_on(date(2026, 1, 15)) == date(2026, 1, 27)
    assert si.published_on(date(2026, 9, 15)) == date(2026, 9, 24)
    assert si.is_due(date(2026, 9, 15), NOW) and not si.is_due(date(2026, 9, 30), NOW)


# --------------------------------------------------------------------------- #
# normalisation
# --------------------------------------------------------------------------- #
def test_frame_from_api_rows_types_flags_and_published_at() -> None:
    rows = [
        _row("2026-09-15", "A"),
        _row(
            "2026-09-15",
            "BRKB",
            revisionFlag="R",
            stockSplitFlag="S",
            marketClassCode="NYSE",
        ),
        _row(
            "2026-09-15",
            "ZZOTC",
            marketClassCode="OTC",
            issuerServicesGroupExchangeCode="S",
        ),
    ]
    frame = si.frame_from_api_rows(date(2026, 9, 15), rows)
    assert list(frame.columns) == si.COLUMNS and len(frame) == 3
    assert (
        frame["short_position"].dtype == "int64"
        and frame["days_to_cover"].dtype == "float64"
    )
    assert frame["revised"].tolist() == [False, True, False]
    assert frame["split_adjusted"].tolist() == [False, True, False]
    assert (pd.to_datetime(frame["published_at"]).dt.date == date(2026, 9, 24)).all()
    assert frame["symbol"].tolist() == [
        "A",
        "BRKB",
        "ZZOTC",
    ]  # sorted, OTC kept as published
    assert si.frame_from_api_rows(date(2026, 9, 15), []).empty


def test_frame_rejects_bad_rows() -> None:
    with pytest.raises(ValueError, match="expected 2026-09-15"):
        si.frame_from_api_rows(date(2026, 9, 15), [_row("2026-08-31", "A")])
    with pytest.raises(ValueError, match="duplicate symbols"):
        si.frame_from_api_rows(
            date(2026, 9, 15), [_row("2026-09-15", "A"), _row("2026-09-15", "A")]
        )
    with pytest.raises(ValueError, match="negative"):
        si.frame_from_api_rows(
            date(2026, 9, 15), [_row("2026-09-15", "A", position=-1)]
        )
    with pytest.raises(ValueError, match="lack fields"):
        si.frame_from_api_rows(date(2026, 9, 15), [{"settlementDate": "2026-09-15"}])


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def test_plan_due_dates_newest_first_with_overlap_by_publication() -> None:
    plan = si.plan_partitions(
        from_date=date(2026, 7, 1), to_date=date(2026, 10, 2), state={}, now=NOW
    )
    assert plan == [
        date(2026, 9, 15),
        date(2026, 8, 31),
        date(2026, 8, 14),
        date(2026, 7, 31),
        date(2026, 7, 15),
    ]
    state = {
        d.isoformat(): DayState(d.isoformat(), "ok", "api", sha256="s") for d in plan
    }
    # 09-15 was published 2026-09-24 (inside a 21-day window from 10-02); 08-31 on
    # 2026-09-10 (Labor Day skipped), one day outside it
    plan = si.plan_partitions(
        from_date=date(2026, 7, 1),
        to_date=date(2026, 10, 2),
        state=state,
        now=NOW,
        overlap_days=21,
    )
    assert plan == [date(2026, 9, 15)]
    plan = si.plan_partitions(
        from_date=date(2026, 7, 1),
        to_date=date(2026, 10, 2),
        state=state,
        now=NOW,
        overlap_days=23,
    )
    assert plan == [date(2026, 9, 15), date(2026, 8, 31)]
    assert (
        si.plan_partitions(
            from_date=date(2026, 7, 1),
            to_date=date(2026, 10, 2),
            state=state,
            now=NOW,
            overlap_days=0,
        )
        == []
    )
    state["2026-07-15"] = DayState("2026-07-15", "absent", "api")
    assert (
        si.plan_partitions(
            from_date=date(2026, 7, 1),
            to_date=date(2026, 10, 2),
            state=state,
            now=NOW,
            overlap_days=0,
        )
        == []
    )
    assert si.plan_partitions(
        from_date=date(2026, 7, 1),
        to_date=date(2026, 10, 2),
        state=state,
        now=NOW,
        overlap_days=0,
        retry_absent=True,
    ) == [date(2026, 7, 15)]


# --------------------------------------------------------------------------- #
# refresh + qc
# --------------------------------------------------------------------------- #
class _Transport:
    name = "api"

    def __init__(self, script: dict) -> None:
        self.script = script
        self.asked: list = []

    def fetch_partition(self, settlement_date):
        self.asked.append(settlement_date)
        outcome = self.script.get(settlement_date)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_refresh_stores_restates_and_qc(tmp_path: Path) -> None:
    d0915, d0831, d0814 = date(2026, 9, 15), date(2026, 8, 31), date(2026, 8, 14)
    frame = si.frame_from_api_rows(
        d0915, [_row("2026-09-15", "A"), _row("2026-09-15", "BRKB")]
    )
    transport = _Transport(
        {d0915: frame, d0831: None, d0814: EntitlementError("no", status=403)}
    )
    report = si.refresh(
        run=False,
        root=tmp_path,
        transport=transport,
        from_date=date(2026, 8, 1),
        now=NOW,
    )
    assert (
        report.planned == ("2026-09-15", "2026-08-31", "2026-08-14")
        and transport.asked == []
    )
    report = si.refresh(
        run=True,
        root=tmp_path,
        transport=transport,
        from_date=date(2026, 8, 1),
        now=NOW,
    )
    assert report.stored == ("2026-09-15",) and report.absent == ("2026-08-31",)
    assert (
        report.failed == (("2026-08-14", "EntitlementError: no (HTTP 403)"),)
        and report.rows == 2
    )
    store = si.store(tmp_path)
    assert (
        store.days_on_disk() == [d0915]
        and store.day_path(d0915).parent.name == "settlement"
    )
    st = store.load_state()["2026-09-15"]
    assert st.rows == 2 and st.short_sum == 2000 and st.total_sum == 200

    revised = si.frame_from_api_rows(
        d0915,
        [
            _row("2026-09-15", "A", position=1500, revisionFlag="R"),
            _row("2026-09-15", "BRKB"),
        ],
    )
    report = si.refresh(
        run=True,
        root=tmp_path,
        transport=_Transport({d0915: revised}),
        from_date=d0915,
        to_date=d0915,
        now=NOW,
    )
    assert report.restated == ("2026-09-15",)
    assert "1 rows flagged R" in store.load_state()["2026-09-15"].detail

    qc = si.qc(tmp_path, now=NOW)
    checks = {f.check: f for f in qc.findings}
    assert (
        qc.ok and qc.days == 1 and checks["errors"].count == 1 and "gaps" not in checks
    )


def test_qc_flags_rule_violations(tmp_path: Path) -> None:
    store = si.store(tmp_path)
    day = date(2026, 9, 15)
    frame = si.frame_from_api_rows(day, [_row("2026-09-15", "A")])
    bad = frame.copy()
    bad["published_at"] = date(2026, 9, 15)  # not the rule
    store.write_day(day, bad)
    store.write_day(
        date(2026, 9, 16), frame
    )  # a file on a date that is no settlement date, rows from 09-15
    qc = si.qc(tmp_path, now=NOW)
    checks = {}
    for f in qc.findings:
        checks.setdefault(f.check, []).append(f)
    assert not qc.ok
    assert checks["published_at"][0].count == 1
    assert any("not a settlement date" in f.message for f in checks["calendar"])
    assert checks["partition"][0].count == 1
