"""Split-neutral share basis (DD-001 section 1).

``F(t)`` is the product of the split ratios with ``ex_date <= t``. Dividing a
raw share count by it and multiplying a raw close by it expresses both in the
share units of the start of the series, so a later split never restates what
was computed before it, and ``quantity * price`` stays the raw market value.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date
from typing import Iterable, Sequence


@dataclass(frozen=True)
class SplitFactor:
    """Cumulative split factor of one security as a step function of the date."""

    ex_dates: tuple[date, ...]
    cumulative: tuple[float, ...]

    @classmethod
    def from_splits(cls, splits: Iterable[tuple[date, float]]) -> "SplitFactor":
        """``splits`` is ``(ex_date, ratio)`` pairs; a 10-for-1 split has ratio 10."""
        ordered = sorted(splits)
        dates: list[date] = []
        cumulative: list[float] = []
        factor = 1.0
        for ex_date, ratio in ordered:
            if not ratio > 0:
                raise ValueError(f"split ratio must be positive, got {ratio} on {ex_date}")
            factor *= float(ratio)
            if dates and dates[-1] == ex_date:
                # two distinct events on one day (a spin-off and a reverse split) compound
                cumulative[-1] = factor
            else:
                dates.append(ex_date)
                cumulative.append(factor)
        return cls(tuple(dates), tuple(cumulative))

    def at(self, on: date) -> float:
        """``F(on)``: 1.0 before the first split, inclusive of a split on ``on``."""
        i = bisect_right(self.ex_dates, on)
        return self.cumulative[i - 1] if i else 1.0

    def quantity(self, raw_shares: float, on: date) -> float:
        return raw_shares / self.at(on)

    def price(self, raw_close: float, on: date) -> float:
        return raw_close * self.at(on)


NO_SPLITS = SplitFactor((), ())

DUPLICATE_WINDOW_DAYS = 5
DUPLICATE_RATIO_TOLERANCE = 0.05


def dedupe_splits(splits: Iterable[tuple[date, float]]) -> list[tuple[date, float]]:
    """Drop vendor duplicates: the same event listed twice.

    Two entries are one event when their ex dates are at most
    ``DUPLICATE_WINDOW_DAYS`` apart and their ratios agree within
    ``DUPLICATE_RATIO_TOLERANCE`` (KB-003: LBTYK 1.91 on two consecutive days,
    NWG 0.928 and 0.928571 on one day). The earliest entry is kept.
    """
    kept: list[tuple[date, float]] = []
    for ex_date, ratio in sorted(splits):
        duplicate = any(
            (ex_date - d).days <= DUPLICATE_WINDOW_DAYS
            and abs(ratio / r - 1.0) <= DUPLICATE_RATIO_TOLERANCE
            for d, r in kept
        )
        if not duplicate:
            kept.append((ex_date, float(ratio)))
    return kept


class PricePath:
    """Closes of one security sorted by date, indexed once for interval queries."""

    def __init__(self, closes: Sequence[tuple[date, float]]) -> None:
        self._dates = [d for d, _ in closes]
        self._values = [v for _, v in closes]

    def interval_mean(self, after: date | None, upto: date) -> float | None:
        """Mean of the closes dated in ``(after, upto]``; ``None`` when there are none.

        With ``after=None`` only the latest close on or before ``upto`` is
        used: the first observation of a series has no interval.
        """
        hi = bisect_right(self._dates, upto)
        if after is None:
            return self._values[hi - 1] if hi else None
        lo = bisect_right(self._dates, after)
        if hi <= lo:
            return None
        # summed directly: a prefix-sum difference loses the digits of a tiny
        # basis price after a vendor spike (999999.9999 placeholders exist)
        window = self._values[lo:hi]
        return sum(window) / len(window)

    def last_close(self, upto: date) -> float | None:
        """The latest close dated on or before ``upto``."""
        hi = bisect_right(self._dates, upto)
        return self._values[hi - 1] if hi else None


def interval_mean(
    closes: Sequence[tuple[date, float]], after: date | None, upto: date
) -> float | None:
    return PricePath(closes).interval_mean(after, upto)


def last_close(closes: Sequence[tuple[date, float]], upto: date) -> float | None:
    return PricePath(closes).last_close(upto)
