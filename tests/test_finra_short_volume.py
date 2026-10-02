"""finra.short_volume: normalisation, symbol mapping, planning, refresh and qc."""

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

from finra import cdn  # noqa: E402
from finra import short_volume as sv  # noqa: E402
from finra.errors import CdnError  # noqa: E402
from finra.store import DayState  # noqa: E402

ET = ZoneInfo("America/New_York")
NOW = datetime(
    2026, 10, 1, 20, 0, tzinfo=ET
)  # Thursday evening: 2026-10-01 is publishable
D = {
    "mon": date(2026, 9, 28),
    "tue": date(2026, 9, 29),
    "wed": date(2026, 9, 30),
    "thu": date(2026, 10, 1),
}


def _daily(
    day: date, rows=(("A", 10, 1, 20, "B,N,Q"), ("BRK/B", 5, 0, 9, "Q"))
) -> cdn.DailyFile:
    return cdn.DailyFile(
        "CNMS",
        day,
        tuple(cdn.DailyRow(*r) for r in rows),
        sha256="x" * 64,
        declared_count=len(rows),
    )


def _frame(
    day: date, rows=(("A", 10, 1, 20, "B,N,Q"), ("BRK/B", 5, 0, 9, "Q"))
) -> pd.DataFrame:
    return sv.frame_from_daily_file(_daily(day, rows))


# --------------------------------------------------------------------------- #
# normalisation + mapping
# --------------------------------------------------------------------------- #
def test_cdn_and_api_rows_normalise_to_the_same_frame() -> None:
    day = D["wed"]
    from_cdn = _frame(day)
    api_rows = [
        {
            "tradeReportDate": "2026-09-30",
            "securitiesInformationProcessorSymbolIdentifier": "A",
            "shortParQuantity": 4.5,
            "shortExemptParQuantity": 1,
            "totalParQuantity": 10.5,
            "marketCode": "Q",
            "reportingFacilityCode": "NQTRF",
        },
        {
            "tradeReportDate": "2026-09-30",
            "securitiesInformationProcessorSymbolIdentifier": "A",
            "shortParQuantity": 3.25,
            "shortExemptParQuantity": 0,
            "totalParQuantity": 6.25,
            "marketCode": "N",
            "reportingFacilityCode": "NYTRF",
        },
        {
            "tradeReportDate": "2026-09-30",
            "securitiesInformationProcessorSymbolIdentifier": "A",
            "shortParQuantity": 2.25,
            "shortExemptParQuantity": 0,
            "totalParQuantity": 3.25,
            "marketCode": "B",
            "reportingFacilityCode": "NCTRF",
        },
        {
            "tradeReportDate": "2026-09-30",
            "securitiesInformationProcessorSymbolIdentifier": "BRK/B",
            "shortParQuantity": 5,
            "shortExemptParQuantity": 0,
            "totalParQuantity": 9,
            "marketCode": "Q",
            "reportingFacilityCode": "NQTRF",
        },
    ]
    from_api = sv.frame_from_api_rows(day, api_rows)
    assert list(from_cdn.columns) == sv.COLUMNS == list(from_api.columns)
    assert from_cdn["source"].tolist() == ["cdn", "cdn"] and from_api[
        "source"
    ].tolist() == ["api", "api"]
    cols = sv.COLUMNS[:-1]
    pd.testing.assert_frame_equal(from_cdn[cols], from_api[cols])
    assert sv.frame_digest(from_cdn) == sv.frame_digest(
        from_api
    )  # source does not change the digest
    assert from_api["short_volume"].dtype == "float64"  # fractional shares are real


def test_api_rows_are_validated() -> None:
    with pytest.raises(ValueError, match="lack fields"):
        sv.frame_from_api_rows(D["wed"], [{"tradeReportDate": "2026-09-30"}])
    rows = [
        {
            "tradeReportDate": "2026-09-29",
            "securitiesInformationProcessorSymbolIdentifier": "A",
            "shortParQuantity": 1,
            "shortExemptParQuantity": 0,
            "totalParQuantity": 2,
            "marketCode": "Q",
        }
    ]
    with pytest.raises(ValueError, match="expected 2026-09-30"):
        sv.frame_from_api_rows(D["wed"], rows)
    assert sv.frame_from_api_rows(D["wed"], []).empty


@pytest.mark.parametrize(
    ("symbol", "kind", "code"),
    [
        ("A", "common", "A"),
        ("BRK/B", "common", "BRK-B"),
        ("ABRpD", "preferred", None),
        ("ACPpA", "preferred", None),
        ("AACT/WS", "warrant", None),
        ("AACT/U", "unit", None),
        ("XYZ/R", "right", None),
        ("weird-1", "other", None),
    ],
)
def test_symbol_kind_and_eodhd_code(symbol: str, kind: str, code: str | None) -> None:
    assert sv.security_kind(symbol) == kind
    assert sv.eodhd_code(symbol) == code


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def _state(**by_day: str) -> dict[str, DayState]:
    return {d: DayState(d, status, "cdn", sha256="s") for d, status in by_day.items()}


def test_plan_is_newest_first_weekdays_up_to_the_latest_publishable() -> None:
    plan = sv.plan_days(
        from_date=date(2026, 9, 25), to_date=date(2026, 10, 5), state={}, now=NOW
    )
    assert plan == [
        D["thu"],
        D["wed"],
        D["tue"],
        D["mon"],
        date(2026, 9, 25),
    ]  # no weekend, no future
    early = datetime(2026, 10, 1, 17, 0, tzinfo=ET)
    assert (
        sv.plan_days(from_date=D["mon"], to_date=D["thu"], state={}, now=early)[0]
        == D["wed"]
    )


def test_plan_skips_ok_days_outside_the_overlap_and_rechecks_inside() -> None:
    state = _state(
        **{
            "2026-09-25": "ok",
            "2026-09-28": "ok",
            "2026-09-29": "ok",
            "2026-09-30": "ok",
        }
    )
    plan = sv.plan_days(
        from_date=date(2026, 9, 25),
        to_date=D["thu"],
        state=state,
        now=NOW,
        overlap_days=2,
    )
    # an overlap of 2 weekdays ending 2026-10-01 covers 09-30 .. 10-01
    assert plan == [D["thu"], D["wed"]]
    assert sv.plan_days(
        from_date=date(2026, 9, 25),
        to_date=D["thu"],
        state=state,
        now=NOW,
        overlap_days=0,
    ) == [D["thu"]]


def test_plan_error_always_absent_only_inside_overlap_or_on_request() -> None:
    state = _state(
        **{
            "2026-09-25": "error",
            "2026-09-28": "absent",
            "2026-09-29": "ok",
            "2026-09-30": "absent",
        }
    )
    plan = sv.plan_days(
        from_date=date(2026, 9, 25),
        to_date=D["thu"],
        state=state,
        now=NOW,
        overlap_days=1,
    )
    assert plan == [
        D["thu"],
        date(2026, 9, 25),
    ]  # 09-30 absent sits outside a 1-day window
    plan = sv.plan_days(
        from_date=date(2026, 9, 25),
        to_date=D["thu"],
        state=state,
        now=NOW,
        overlap_days=1,
        retry_absent=True,
    )
    assert plan == [
        D["thu"],
        D["wed"],
        D["mon"],
        date(2026, 9, 25),
    ]  # absent days on request
    full = sv.plan_days(
        from_date=date(2026, 9, 25),
        to_date=D["thu"],
        state=state,
        now=NOW,
        full_refresh=True,
    )
    assert len(full) == 5


def test_plan_empty_when_range_is_in_the_future() -> None:
    assert (
        sv.plan_days(
            from_date=date(2026, 10, 2), to_date=date(2026, 10, 9), state={}, now=NOW
        )
        == []
    )


# --------------------------------------------------------------------------- #
# refresh with a scripted transport
# --------------------------------------------------------------------------- #
class _Transport:
    name = "cdn"

    def __init__(self, script: dict[date, object]) -> None:
        self.script = script
        self.asked: list[date] = []

    def fetch_day(self, trade_date: date):
        self.asked.append(trade_date)
        outcome = self.script.get(trade_date, None)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_dry_run_plans_and_touches_nothing(tmp_path: Path) -> None:
    transport = _Transport({})
    report = sv.refresh(
        run=False, root=tmp_path, transport=transport, from_date=D["mon"], now=NOW
    )
    assert not report.run and report.planned == (D["thu"], D["wed"], D["tue"], D["mon"])
    assert transport.asked == [] and not (tmp_path / "short_volume").exists()
    assert report.not_yet_publishable is None
    early = datetime(2026, 10, 1, 12, 0, tzinfo=ET)
    report = sv.refresh(
        run=False,
        root=tmp_path,
        transport=transport,
        from_date=D["mon"],
        now=early,
        limit_days=2,
    )
    assert report.planned == (D["wed"], D["tue"]) and report.skipped == (D["mon"],)
    assert report.not_yet_publishable == D["thu"]


def test_run_stores_days_records_absent_and_errors_and_resumes(tmp_path: Path) -> None:
    transport = _Transport(
        {
            D["thu"]: _frame(D["thu"]),
            D["wed"]: None,
            D["tue"]: CdnError("boom", status=503),
            D["mon"]: _frame(D["mon"]),
        }
    )
    report = sv.refresh(
        run=True, root=tmp_path, transport=transport, from_date=D["mon"], now=NOW
    )
    assert report.stored == (D["thu"], D["mon"]) and report.absent == (D["wed"],)
    assert report.failed == ((D["tue"], "CdnError: boom (HTTP 503)"),)
    assert report.rows == 4 and not report.ok and not report.aborted
    store = sv.store(tmp_path)
    assert store.days_on_disk() == [D["mon"], D["thu"]]
    state = store.load_state()
    assert state["2026-10-01"].status == "ok" and state["2026-10-01"].rows == 2
    assert state["2026-10-01"].short_sum == 15 and state["2026-10-01"].total_sum == 29
    assert (
        state["2026-09-30"].status == "absent"
        and state["2026-09-30"].detail == "no file published"
    )
    assert (
        state["2026-09-29"].status == "error" and "boom" in state["2026-09-29"].detail
    )
    back = store.read_day(D["thu"])
    assert (
        back is not None
        and back["symbol"].tolist() == ["A", "BRK/B"]
        and back["source"].iloc[0] == "cdn"
    )

    # next run: the error day is retried, the ok day inside the overlap re-checked, the rest left alone
    transport = _Transport(
        {D["tue"]: _frame(D["tue"]), D["thu"]: _frame(D["thu"]), D["wed"]: None}
    )
    report = sv.refresh(
        run=True,
        root=tmp_path,
        transport=transport,
        from_date=D["mon"],
        now=NOW,
        overlap_days=1,
    )
    assert transport.asked == [
        D["thu"],
        D["tue"],
    ]  # wed (absent) is outside a 1-day overlap
    assert report.ok and report.stored == (D["thu"], D["tue"]) and report.restated == ()


def test_restatement_is_detected_inside_the_overlap(tmp_path: Path) -> None:
    transport = _Transport({D["thu"]: _frame(D["thu"])})
    sv.refresh(
        run=True, root=tmp_path, transport=transport, from_date=D["thu"], now=NOW
    )
    changed = _frame(D["thu"], (("A", 11, 1, 20, "B,N,Q"), ("BRK/B", 5, 0, 9, "Q")))
    report = sv.refresh(
        run=True,
        root=tmp_path,
        transport=_Transport({D["thu"]: changed}),
        from_date=D["thu"],
        now=NOW,
    )
    assert report.restated == (D["thu"],) and report.stored == (D["thu"],)
    state = sv.store(tmp_path).load_state()["2026-10-01"]
    assert state.status == "ok" and state.short_sum == 16
    assert state.detail.startswith("restated") and "short_sum=15" in state.detail
    # an identical re-fetch keeps the restatement note and does not report again
    report = sv.refresh(
        run=True,
        root=tmp_path,
        transport=_Transport({D["thu"]: changed}),
        from_date=D["thu"],
        now=NOW,
    )
    assert report.restated == () and sv.store(tmp_path).load_state()[
        "2026-10-01"
    ].detail.startswith("restated")


def test_absent_streak_aborts_as_error_not_holidays(tmp_path: Path) -> None:
    transport = _Transport({})  # everything absent: a block or an outage
    report = sv.refresh(
        run=True,
        root=tmp_path,
        transport=transport,
        from_date=date(2026, 9, 21),
        now=NOW,
    )
    assert report.aborted.startswith(
        "3 consecutive weekdays absent (2026-09-29..2026-10-01)"
    )
    assert transport.asked == [D["thu"], D["wed"], D["tue"]]  # stopped at the third
    assert report.absent == () and {d for d, _ in report.failed} == {
        D["thu"],
        D["wed"],
        D["tue"],
    }
    state = sv.store(tmp_path).load_state()
    assert all(
        state[d].status == "error" and "absent streak" in state[d].detail
        for d in ("2026-09-29", "2026-09-30", "2026-10-01")
    )
    assert not report.ok


def test_two_absent_days_around_a_stored_day_do_not_trip_the_guard(
    tmp_path: Path,
) -> None:
    transport = _Transport(
        {D["wed"]: _frame(D["wed"]), D["mon"]: _frame(D["mon"])}
    )  # thu, tue absent
    report = sv.refresh(
        run=True, root=tmp_path, transport=transport, from_date=D["mon"], now=NOW
    )
    assert (
        report.ok
        and report.absent == (D["thu"], D["tue"])
        and report.stored == (D["wed"], D["mon"])
    )


def test_unexpected_exceptions_are_recorded_not_raised(tmp_path: Path) -> None:
    transport = _Transport({D["thu"]: RuntimeError("socket gone")})
    report = sv.refresh(
        run=True, root=tmp_path, transport=transport, from_date=D["thu"], now=NOW
    )
    assert report.failed == ((D["thu"], "RuntimeError: socket gone"),)


# --------------------------------------------------------------------------- #
# qc
# --------------------------------------------------------------------------- #
def test_qc_clean_store_and_eodhd_coverage(tmp_path: Path) -> None:
    transport = _Transport(
        {D["thu"]: _frame(D["thu"]), D["wed"]: None, D["tue"]: _frame(D["tue"])}
    )
    sv.refresh(
        run=True, root=tmp_path, transport=transport, from_date=D["tue"], now=NOW
    )
    report = sv.qc(tmp_path, eodhd_codes=["A", "BRK-B", "ZZZ"])
    assert (
        report.ok
        and report.days == 2
        and (report.first, report.last) == (D["tue"], D["thu"])
    )
    checks = {f.check: f for f in report.findings}
    assert set(checks) == {"eodhd_coverage"}  # wed is absent, so no gap
    assert (
        checks["eodhd_coverage"].count == 1
        and "ZZZ" in checks["eodhd_coverage"].message
    )
    assert checks["eodhd_coverage"].severity == "warn"  # 1 of 3 is above the 5% line


def test_qc_finds_bad_rows_gaps_and_state_drift(tmp_path: Path) -> None:
    store = sv.store(tmp_path)
    good = _frame(D["mon"])
    store.write_day(D["mon"], good)
    bad = _frame(D["thu"]).copy()
    bad.loc[0, "short_volume"] = 999  # short > total
    bad.loc[1, "short_exempt_volume"] = 7  # exempt > short
    store.write_day(D["thu"], bad)
    store.upsert_state(
        [
            DayState("2026-09-28", "ok", "cdn", rows=5),
            DayState("2026-09-29", "error", "cdn"),
            DayState("2026-10-02", "ok", "cdn", rows=1),
        ]
    )
    report = sv.qc(tmp_path)
    assert not report.ok
    by = {}
    for f in report.findings:
        by.setdefault(f.check, []).append(f)
    assert (
        by["short_gt_total"][0].severity == "error"
        and by["exempt_gt_short"][0].count == 1
    )
    assert "state says 5 rows, file holds 2" in by["state"][0].message
    assert any("file on disk but state is missing" in f.message for f in by["state"])
    assert any("state ok but no file on disk" in f.message for f in by["state"])
    assert by["errors"][0].count == 1
    assert (
        by["gaps"][0].count == 2
    )  # tue (error) and wed (no state) between mon and thu


def test_qc_empty_store(tmp_path: Path) -> None:
    report = sv.qc(tmp_path)
    assert report.ok and report.days == 0 and report.findings[0].check == "store"
