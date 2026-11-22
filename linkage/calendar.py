"""Sessions, month ends and quarters. Pure."""

from __future__ import annotations

import pandas as pd


def month_end_sessions(
    sessions: pd.DatetimeIndex, start: pd.Timestamp | None = None
) -> pd.DatetimeIndex:
    """The last session of every month that is complete in ``sessions``.

    A month counts as complete once a later session exists, so a month whose
    last days are not loaded yet never yields a snapshot dated mid-month.
    """
    if len(sessions) == 0:
        return pd.DatetimeIndex([])
    s = pd.DatetimeIndex(sorted(set(sessions)))
    months = s.to_period("M")
    last = pd.Series(s, index=months).groupby(level=0).max()
    complete = last[last.index < months[-1]]
    out = pd.DatetimeIndex(complete.to_numpy())
    return out[out >= start] if start is not None else out


def quarter_start(day: pd.Timestamp) -> pd.Timestamp:
    """The first calendar day of ``day``'s quarter (the membership key)."""
    return pd.Timestamp(day).to_period("Q").start_time.normalize()


def window(
    sessions: pd.DatetimeIndex, end: pd.Timestamp, length: int
) -> pd.DatetimeIndex:
    """The last ``length`` sessions on or before ``end``."""
    upto = sessions[sessions <= end]
    return upto[-length:]
