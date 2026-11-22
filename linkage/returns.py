"""Daily total returns that survive the vendor's corporate-action conventions. Pure.

The eodhd daily ``close`` is raw for most names and already adjusted for
serial reverse-splitters, and ``adjusted_close`` is not re-adjusted by the bulk
top-up, so no single price column gives a correct return across a split.
Betas do not need every day, so the rule is to drop what cannot be trusted:

- a return is ``(close_t + dividend_t) / close_{t-1} - 1`` between two
  consecutive market sessions; a name that skipped a session has no return
  for the day it came back (``gap``);
- every session within ``event_window`` of a split ex-date is dropped
  (``event``), whichever way the vendor stored the split;
- a dividend above ``max_dividend_yield`` of the previous close drops the
  day (``dividend``: a mis-scaled amount or a special that is not a return);
- a remaining move larger than ``max_abs_log_return`` in log terms is dropped
  (``jump``): almost always a split the events file does not have yet.

Every dropped day is counted per ticker, so ``qc`` can show what was masked.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

REASONS = ("gap", "event", "dividend", "jump")


@dataclass(frozen=True)
class ReturnsPanel:
    """Wide panels on one session index; columns are tickers."""

    returns: pd.DataFrame  # float32 total returns, NaN where missing or masked
    close: pd.DataFrame  # float32
    volume: pd.DataFrame  # float32 (restated for later splits by the vendor)
    masked: pd.DataFrame  # per ticker: one count column per reason

    @property
    def sessions(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.returns.index)


def total_returns(
    prices: pd.DataFrame,
    splits: pd.DataFrame,
    dividends: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    *,
    event_window: int,
    max_abs_log_return: float,
    max_dividend_yield: float,
) -> ReturnsPanel:
    """Build the panels from long ``prices`` (``ticker, date, close, volume``)."""
    sessions = pd.DatetimeIndex(sorted(set(pd.DatetimeIndex(sessions))))
    tickers = pd.Index(sorted(prices["ticker"].unique()))
    frame = prices[prices["date"].isin(sessions)].sort_values(
        ["ticker", "date"], ignore_index=True
    )
    si = sessions.get_indexer(pd.DatetimeIndex(frame["date"]))
    ti = tickers.get_indexer(pd.Index(frame["ticker"]))
    close = frame["close"].to_numpy(dtype=np.float64)
    same = np.r_[False, ti[1:] == ti[:-1]]
    prev_close = np.where(same, np.r_[np.nan, close[:-1]], np.nan)
    prev_si = np.where(same, np.r_[-1, si[:-1]], -1)

    amount = np.zeros(len(frame))
    if not dividends.empty:
        div = dividends.groupby(["ticker", "ex_date"])["amount"].sum()
        div.index = div.index.set_names(["ticker", "date"])
        keyed = frame[["ticker", "date"]].join(div, on=["ticker", "date"])
        amount = keyed["amount"].fillna(0.0).to_numpy(dtype=np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        ret = (close + amount) / prev_close - 1.0
    reason = np.full(len(frame), "", dtype=object)
    gap = same & (si - prev_si != 1)
    reason[gap] = "gap"
    ret[~same | gap] = np.nan

    with np.errstate(divide="ignore", invalid="ignore"):
        big_dividend = (amount / prev_close) > max_dividend_yield
    reason[big_dividend & (reason == "")] = "dividend"
    ret[big_dividend] = np.nan

    event = _event_mask(frame, si, ti, splits, sessions, tickers, event_window)
    reason[event & (reason == "") & same] = "event"
    ret[event] = np.nan

    with np.errstate(divide="ignore", invalid="ignore"):
        jump = np.abs(np.log1p(ret)) > max_abs_log_return
    jump &= ~np.isnan(ret)
    reason[jump] = "jump"
    ret[jump] = np.nan

    shape = (len(sessions), len(tickers))
    panels = {}
    for name, values in (
        ("returns", ret),
        ("close", close),
        ("volume", frame["volume"].to_numpy(dtype=np.float64)),
    ):
        grid = np.full(shape, np.nan, dtype=np.float32)
        grid[si, ti] = values
        panels[name] = pd.DataFrame(grid, index=sessions, columns=tickers)
    counts = pd.DataFrame({"ticker": frame["ticker"], "reason": reason})
    counts = counts[counts["reason"] != ""]
    masked = (
        counts.groupby(["ticker", "reason"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=list(REASONS), fill_value=0)
        if not counts.empty
        else pd.DataFrame(columns=list(REASONS))
    )
    return ReturnsPanel(
        panels["returns"], panels["close"], panels["volume"], masked.astype("int64")
    )


def _event_mask(
    frame: pd.DataFrame,
    si: np.ndarray,
    ti: np.ndarray,
    splits: pd.DataFrame,
    sessions: pd.DatetimeIndex,
    tickers: pd.Index,
    width: int,
) -> np.ndarray:
    """Rows within ``width`` sessions of a split ex-date of their ticker."""
    mask = np.zeros(len(frame), dtype=bool)
    if splits.empty:
        return mask
    known = splits[splits["ticker"].isin(tickers)]
    if known.empty:
        return mask
    # the ex-date's session: the first session on or after it
    ex_si = sessions.searchsorted(pd.DatetimeIndex(known["ex_date"]))
    ex_ti = tickers.get_indexer(pd.Index(known["ticker"]))
    flagged = np.zeros((len(tickers), len(sessions) + 2 * width + 1), dtype=bool)
    for offset in range(-width, width + 1):
        cols = ex_si + offset + width
        flagged[ex_ti, np.clip(cols, 0, flagged.shape[1] - 1)] = True
    return flagged[ti, si + width]
