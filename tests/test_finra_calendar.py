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


# --------------------------------------------------------------------------- #
# NYSE holidays and business days
# --------------------------------------------------------------------------- #
# Every weekday the daily short volume store recorded as absent, 2018-08-01 to
# 2026-09-30: the ground truth the rule-based calendar must reproduce.
STORE_ABSENT = """2018-09-03 2018-11-22 2018-12-05 2018-12-25 2019-01-01 2019-01-21 2019-02-18
2019-04-19 2019-05-27 2019-07-04 2019-09-02 2019-11-28 2019-12-25 2020-01-01 2020-01-20
2020-02-17 2020-04-10 2020-05-25 2020-07-03 2020-09-07 2020-11-26 2020-12-25 2021-01-01
2021-01-18 2021-02-15 2021-04-02 2021-05-31 2021-07-05 2021-09-06 2021-11-25 2021-12-24
2022-01-17 2022-02-21 2022-04-15 2022-05-30 2022-06-20 2022-07-04 2022-09-05 2022-11-24
2022-12-26 2023-01-02 2023-01-16 2023-02-20 2023-04-07 2023-05-29 2023-06-19 2023-07-04
2023-09-04 2023-11-23 2023-12-25 2024-01-01 2024-01-15 2024-02-19 2024-03-29 2024-05-27
2024-06-19 2024-07-04 2024-09-02 2024-11-28 2024-12-25 2025-01-01 2025-01-09 2025-01-20
2025-02-17 2025-04-18 2025-05-26 2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19 2026-07-03 2026-09-07""".split()


def test_holiday_rules_reproduce_every_absent_weekday_in_the_store() -> None:
    start, end = date(2018, 8, 1), date(2026, 9, 30)
    by_rule = {
        d
        for year in range(start.year, end.year + 1)
        for d in cal.nyse_holidays(year)
        if start <= d <= end and cal.is_weekday(d)
    }
    assert by_rule == {date.fromisoformat(s) for s in STORE_ABSENT}


def test_observance_rules() -> None:
    assert date(2021, 12, 24) in cal.nyse_holidays(2021)  # Christmas on a Saturday
    assert date(2022, 12, 26) in cal.nyse_holidays(2022)  # Christmas on a Sunday
    assert date(2021, 12, 31) not in cal.nyse_holidays(
        2021
    )  # New Year 2022 on a Saturday: no Friday
    assert date(2022, 6, 20) in cal.nyse_holidays(2022)  # Juneteenth on a Sunday
    assert date(2021, 6, 18) not in cal.nyse_holidays(2021)  # not observed before 2022
    assert cal.easter(2024) == date(2024, 3, 31) and cal.easter(2026) == date(
        2026, 4, 5
    )


def test_business_days_after_reproduces_finra_short_interest_schedule() -> None:
    # settlement + 7 business days = FINRA's publication date, four schedule rows
    for settlement, published in (
        ("2025-11-14", "2025-11-25"),
        ("2025-12-15", "2025-12-24"),
        ("2025-12-31", "2026-01-12"),  # New Year skipped
        ("2026-01-15", "2026-01-27"),  # MLK Day skipped
    ):
        assert cal.business_days_after(
            date.fromisoformat(settlement), 7
        ) == date.fromisoformat(published)
    assert cal.business_days_after(date(2026, 10, 2), 0) == date(2026, 10, 2)
    assert cal.next_trading_day(date(2026, 7, 2)) == date(
        2026, 7, 6
    )  # July 3 observed, weekend
    assert not cal.is_trading_day(date(2026, 9, 7)) and cal.is_trading_day(
        date(2026, 9, 8)
    )
    import pytest

    with pytest.raises(ValueError):
        cal.business_days_after(date(2026, 1, 1), -1)
