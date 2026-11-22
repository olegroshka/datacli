"""Quiet windows: times of day when this machine's Alpaca key is left alone.

The key pair is shared with the paper book, which fetches the day's bars
around 15:45 New York. A backfill running then could take its rate budget, so
the fetcher does not start a request inside a quiet window. Windows are
``HH:MM-HH:MM`` in one timezone and apply Monday to Friday (holidays are not
special-cased: being quiet on a holiday costs nothing).
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class QuietWindow:
    start: dt.time
    end: dt.time

    @classmethod
    def parse(cls, text: str) -> "QuietWindow":
        """``"15:30-16:15"`` -> a window; ``ValueError`` on anything else."""
        try:
            left, right = (part.strip() for part in text.split("-", 1))
            start, end = dt.time.fromisoformat(left), dt.time.fromisoformat(right)
        except ValueError as exc:
            raise ValueError(
                f"quiet window must look like HH:MM-HH:MM, got {text!r}"
            ) from exc
        if not start < end:
            raise ValueError(f"quiet window must start before it ends, got {text!r}")
        return cls(start, end)

    def contains(self, moment: dt.time) -> bool:
        return self.start <= moment < self.end

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


class QuietSchedule:
    """A set of weekday windows in one timezone."""

    def __init__(self, windows: Iterable[str], timezone: str) -> None:
        self.windows = tuple(QuietWindow.parse(w) for w in windows)
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)

    def active(self, now: dt.datetime) -> QuietWindow | None:
        """The window ``now`` (timezone-aware) falls in, or ``None``."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        local = now.astimezone(self._zone)
        if local.weekday() >= 5:
            return None
        moment = local.time().replace(tzinfo=None)
        return next((w for w in self.windows if w.contains(moment)), None)

    def describe(self) -> str:
        if not self.windows:
            return "none"
        return ", ".join(str(w) for w in self.windows) + f" {self.timezone}, Mon-Fri"
