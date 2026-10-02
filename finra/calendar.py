"""The publication clock for FINRA's daily files.

FINRA posts the daily short sale volume files "no later than 6:00 pm ET on
the trade date". A trade date is therefore *publishable* once it is a weekday
and 18:00 Eastern has passed on that date; before that no request is worth
making and no state row should exist. Everything here is pure, takes the
current time as an argument, and is unit-tested across the DST edges.

``America/New_York`` comes from :mod:`zoneinfo`; on Windows the IANA database
is provided by the ``tzdata`` package, which pandas already depends on.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterator
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
PUBLISH_TIME = time(18, 0)


def now_et() -> datetime:
    """The current wall-clock time in Eastern Time (tz-aware)."""
    return datetime.now(timezone.utc).astimezone(ET)


def to_et(moment: datetime) -> datetime:
    """``moment`` in Eastern Time; a naive datetime is taken as already Eastern."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=ET)
    return moment.astimezone(ET)


def is_weekday(day: date) -> bool:
    return day.weekday() < 5


def previous_weekday(day: date) -> date:
    cursor = day - timedelta(days=1)
    while not is_weekday(cursor):
        cursor -= timedelta(days=1)
    return cursor


def next_weekday(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_weekday(cursor):
        cursor += timedelta(days=1)
    return cursor


def weekdays(start: date, end: date) -> Iterator[date]:
    """Every weekday from ``start`` to ``end`` inclusive, ascending."""
    cursor = start
    while cursor <= end:
        if is_weekday(cursor):
            yield cursor
        cursor += timedelta(days=1)


def weekdays_back(day: date, n: int) -> date:
    """The weekday ``n`` weekdays before ``day`` (``n = 0`` is ``day`` itself)."""
    cursor = day
    for _ in range(max(n, 0)):
        cursor = previous_weekday(cursor)
    return cursor


def is_publishable(day: date, now: datetime) -> bool:
    """True once FINRA's deadline for ``day`` has passed (a weekday, 18:00 ET)."""
    if not is_weekday(day):
        return False
    local = to_et(now)
    if day < local.date():
        return True
    return day == local.date() and local.timetz().replace(tzinfo=None) >= PUBLISH_TIME


def latest_publishable(now: datetime) -> date:
    """The most recent trade date FINRA is due to have published at ``now``."""
    local = to_et(now)
    today = local.date()
    if is_publishable(today, local):
        return today
    return previous_weekday(today)


# --------------------------------------------------------------------------- #
# NYSE holidays and business days
# --------------------------------------------------------------------------- #
#: Closures that no rule produces: national days of mourning.
SPECIAL_CLOSURES: frozenset[date] = frozenset(
    {
        date(2018, 12, 5),  # George H. W. Bush
        date(2025, 1, 9),  # Jimmy Carter
    }
)


def easter(year: int) -> date:
    """Easter Sunday (Gregorian, anonymous algorithm)."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month, day = divmod(h + l - 7 * m + 114, 31)
    return date(year, month, day + 1)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """The ``n``-th (1-based) ``weekday`` (Mon=0) of the month."""
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date | None:
    """NYSE observance: Saturday -> Friday, Sunday -> Monday, with the New Year
    exception (a Saturday New Year's Day is not observed on the Friday)."""
    if day.weekday() == 5:
        return None if (day.month, day.day) == (1, 1) else day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    """The NYSE full-day closures of ``year`` by rule, plus the special closures."""
    fixed = [date(year, 1, 1), date(year, 7, 4), date(year, 12, 25)]
    if year >= 2022:
        fixed.append(date(year, 6, 19))  # Juneteenth, observed by the NYSE since 2022
    days = {d for d in (_observed(f) for f in fixed) if d is not None}
    days.add(_nth_weekday(year, 1, 0, 3))  # Martin Luther King Jr. Day
    days.add(_nth_weekday(year, 2, 0, 3))  # Presidents' Day
    days.add(easter(year) - timedelta(days=2))  # Good Friday
    days.add(_last_weekday(year, 5, 0))  # Memorial Day
    days.add(_nth_weekday(year, 9, 0, 1))  # Labor Day
    days.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving
    days |= {d for d in SPECIAL_CLOSURES if d.year == year}
    return {d for d in days if d.year == year}


def is_trading_day(day: date) -> bool:
    return is_weekday(day) and day not in nyse_holidays(day.year)


def next_trading_day(day: date) -> date:
    cursor = day + timedelta(days=1)
    while not is_trading_day(cursor):
        cursor += timedelta(days=1)
    return cursor


def business_days_after(day: date, n: int) -> date:
    """The trading day ``n`` trading days after ``day`` (``n = 0`` is ``day``)."""
    if n < 0:
        raise ValueError("n must be >= 0")
    cursor = day
    for _ in range(n):
        cursor = next_trading_day(cursor)
    return cursor
