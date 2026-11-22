"""Does the stored auction data cover what the store prices? Pure functions.

A fetch can succeed and still hold little: Alpaca keys auctions by security,
and the store's daily series of a ticker may run through a predecessor
security, an OTC stretch or a rename. Measures per ticker, over its extent
and against the store's own trading days for it, leaving out the sessions of
reviewed ``[[gap]]`` stretches:

- ``coverage``: the share of the store's sessions with a closing cross;
- ``longest_gap``: the longest run of store sessions without one, anywhere
  (a predecessor security leaves one at the start, a mid-life change of
  security one in the middle);
- ``late_sessions``: the part of it before the first closing cross.

A ticker is ``ok`` when ``coverage >= min_coverage`` and ``longest_gap <=
max_gap_sessions``, else ``thin``. :func:`verify_closes` is the check an
alias segment must pass before it is stored: its closing crosses must match
the store's daily closes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np
import pandas as pd

from alpaca import auctions as au

OK, THIN = "ok", "thin"
COLUMNS = [
    "ticker",
    "extent_from",
    "extent_to",
    "expected_days",
    "close_days",
    "coverage",
    "first_close",
    "late_sessions",
    "longest_gap",
    "gap_from",
    "gap_to",
    "reviewed_gap_sessions",
    "status",
]


def assess(
    daily: pd.DataFrame,
    store_days: pd.DataFrame,
    extents: Mapping[str, tuple[dt.date, dt.date]],
    *,
    min_coverage: float,
    max_gap_sessions: int,
    gaps: Mapping[str, Sequence[tuple[dt.date, dt.date]]] | None = None,
) -> pd.DataFrame:
    """One row per ticker of ``extents``.

    ``daily`` is the auctions daily file (``ticker, date, close_price``);
    ``store_days`` holds the store's trading days (``ticker, date``).
    """
    closes = daily.loc[daily["close_price"].notna(), ["ticker", "date"]]
    by_close = {t: set(g["date"]) for t, g in closes.groupby("ticker")}
    by_store = {t: sorted(set(g["date"])) for t, g in store_days.groupby("ticker")}
    reviewed = gaps or {}
    rows = []
    for ticker, (start, end) in sorted(extents.items()):
        spans = reviewed.get(ticker, ())
        inside = [d for d in by_store.get(ticker, []) if start <= d <= end]
        sessions = [d for d in inside if not any(a <= d <= b for a, b in spans)]
        crossed = by_close.get(ticker, set())
        hit = [d in crossed for d in sessions]
        expected, got = len(sessions), int(sum(hit))
        first = next((d for d, h in zip(sessions, hit) if h), None)
        late = hit.index(True) if True in hit else expected
        share = got / expected if expected else 1.0
        gap, gap_at = _longest_miss(hit)
        gap_from = sessions[gap_at] if gap else None
        gap_to = sessions[gap_at + gap - 1] if gap else None
        status = OK if share >= min_coverage and gap <= max_gap_sessions else THIN
        rows.append(
            (
                ticker,
                start,
                end,
                expected,
                got,
                round(share, 4),
                first,
                late,
                gap,
                gap_from,
                gap_to,
                len(inside) - expected,
                status,
            )
        )
    return pd.DataFrame(rows, columns=COLUMNS)


def _longest_miss(hit: Sequence[bool]) -> tuple[int, int]:
    """The longest run of consecutive ``False`` and the index where it starts."""
    longest = run = 0
    at = 0
    for i, value in enumerate(hit):
        run = 0 if value else run + 1
        if run > longest:
            longest, at = run, i - run + 1
    return longest, at


@dataclass(frozen=True)
class Match:
    compared: int  # days with both a closing cross and a store close
    matched: int

    @property
    def share(self) -> float:
        return self.matched / self.compared if self.compared else 0.0


def verify_closes(
    prints: pd.DataFrame, store_closes: pd.DataFrame, *, tolerance: float
) -> Match:
    """How many days' chosen closing cross is within ``tolerance`` of the store's close.

    ``store_closes`` has ``date`` and ``close``.
    """
    daily = au.select_crosses(prints)
    daily = daily[daily["close_price"].notna()]
    if daily.empty or store_closes.empty:
        return Match(0, 0)
    joined = daily[["date", "close_price"]].merge(
        store_closes[["date", "close"]], on="date", how="inner"
    )
    joined = joined[joined["close"] > 0]
    if joined.empty:
        return Match(0, 0)
    ratio = joined["close_price"].to_numpy(dtype=float) / joined["close"].to_numpy(
        dtype=float
    )
    return Match(len(joined), int((np.abs(ratio - 1.0) <= tolerance).sum()))
