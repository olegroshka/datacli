"""finra.calendar: the 18:00 ET publication clock across weekends and DST."""

from __future__ import annotations

import sys
from datetime import date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import calendar as cal  # noqa: E402

ET = ZoneInfo("America/New_York")
LONDON = ZoneInfo("Europe/London")


def _et(y: int, m: int, d: int, hh: int, mm: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def test_weekday_helpers() -> None:
    fri, sat, mon = date(2026, 10, 2), date(2026, 10, 3), date(2026, 10, 5)
    assert cal.is_weekday(fri) and not cal.is_weekday(sat)
    assert cal.previous_weekday(mon) == fri and cal.next_weekday(fri) == mon
    assert list(cal.weekdays(fri, mon)) == [fri, mon]
    assert cal.weekdays_back(mon, 0) == mon and cal.weekdays_back(mon, 2) == date(
        2026, 10, 1
    )


def test_publishable_turns_at_18_00_et() -> None:
    thu = date(2026, 10, 1)
    assert not cal.is_publishable(thu, _et(2026, 10, 1, 17, 59))
    assert cal.is_publishable(thu, _et(2026, 10, 1, 18, 0))
    assert cal.is_publishable(
        date(2026, 9, 30), _et(2026, 10, 1, 9)
    )  # yesterday: always
    assert not cal.is_publishable(
        date(2026, 10, 2), _et(2026, 10, 1, 23)
    )  # tomorrow: never
    assert not cal.is_publishable(
        date(2026, 10, 3), _et(2026, 10, 5, 12)
    )  # a Saturday: never


def test_latest_publishable_skips_weekends_and_today_before_deadline() -> None:
    assert cal.latest_publishable(_et(2026, 10, 1, 17)) == date(2026, 9, 30)
    assert cal.latest_publishable(_et(2026, 10, 1, 18, 30)) == date(2026, 10, 1)
    assert cal.latest_publishable(_et(2026, 10, 3, 12)) == date(
        2026, 10, 2
    )  # Saturday -> Friday
    assert cal.latest_publishable(_et(2026, 10, 5, 8)) == date(
        2026, 10, 2
    )  # Monday morning -> Friday


def test_other_zones_are_converted_to_eastern() -> None:
    # 23:30 London on 2026-10-01 is 18:30 ET (5h apart): publishable
    assert cal.is_publishable(
        date(2026, 10, 1), datetime(2026, 10, 1, 23, 30, tzinfo=LONDON)
    )
    # 22:30 London is 17:30 ET: not yet
    assert not cal.is_publishable(
        date(2026, 10, 1), datetime(2026, 10, 1, 22, 30, tzinfo=LONDON)
    )
    # UTC input works too; naive input is taken as Eastern
    assert cal.is_publishable(
        date(2026, 10, 1), datetime(2026, 10, 1, 22, 0, tzinfo=timezone.utc)
    )
    assert cal.is_publishable(date(2026, 10, 1), datetime(2026, 10, 1, 18, 0))


def test_dst_mismatch_weeks_keep_23_30_london_after_the_deadline() -> None:
    # US springs forward 2026-03-08, the UK on 2026-03-29: London is only 4h ahead
    assert cal.is_publishable(
        date(2026, 3, 16), datetime(2026, 3, 16, 23, 30, tzinfo=LONDON)
    )
    # UK falls back 2026-10-25, the US on 2026-11-01: again 4h ahead
    assert cal.is_publishable(
        date(2026, 10, 28), datetime(2026, 10, 28, 23, 30, tzinfo=LONDON)
    )


def test_now_et_is_aware() -> None:
    now = cal.now_et()
    assert now.tzinfo is not None and now.utcoffset() is not None
