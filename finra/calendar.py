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
