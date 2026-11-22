"""One month-end snapshot of trailing ETF betas. Pure.

For every stock eligible at ``as_of`` and every context ETF, over the last
``window`` sessions up to and including ``as_of``:

- ``beta`` / ``corr``: the stock's daily total return on the ETF's;
- ``beta_ex_mkt`` / ``corr_ex_mkt``: the same after taking the market (SPY)
  out of both legs, each by its own OLS fit over the window. Raw betas to a
  sector ETF are mostly market beta; the net ones say whether the stock moves
  with the sector beyond the market, which is the nearest thing to
  membership that prices can show. They are NULL for the market row itself.

A stock is eligible when, over the window, its median close is at least
``min_price``, its median close * volume at least ``min_dollar_volume`` and it
has a return on at least ``min_obs`` sessions. A pair needs ``min_obs`` shared
sessions. Everything is computed from data dated on or before ``as_of``.
"""

from __future__ import annotations

from typing import Mapping

import numpy as np
import pandas as pd
import pyarrow as pa

from linkage.calendar import window as window_of
from linkage.config import BetaParams
from linkage.moments import market_residuals, pairwise
from linkage.returns import ReturnsPanel

SCHEMA = pa.schema(
    [
        ("as_of", pa.date32()),
        ("ticker", pa.string()),
        ("etf", pa.string()),
        ("etf_group", pa.string()),
        ("window", pa.int16()),
        ("n_obs", pa.int16()),
        ("beta", pa.float32()),
        ("corr", pa.float32()),
        ("beta_ex_mkt", pa.float32()),
        ("corr_ex_mkt", pa.float32()),
    ]
)
COLUMNS = list(SCHEMA.names)


def eligible(
    stocks: ReturnsPanel, rows: pd.DatetimeIndex, params: BetaParams, window: int
) -> pd.Index:
    close = stocks.close.loc[rows]
    dollar = close * stocks.volume.loc[rows]
    count = stocks.returns.loc[rows].notna().sum()
    keep = (
        (close.median() >= params.min_price)
        & (dollar.median() >= params.min_dollar_volume)
        & (count >= params.min_obs(window))
    )
    return keep[keep].index


def snapshot(
    stocks: ReturnsPanel,
    etf_returns: pd.DataFrame,
    etf_groups: Mapping[str, str],
    as_of: pd.Timestamp,
    params: BetaParams,
) -> pd.DataFrame:
    """The long table of every eligible (stock, ETF, window) at ``as_of``."""
    if params.market not in etf_returns.columns:
        raise ValueError(f"the market {params.market!r} is not among the ETF returns")
    sessions = stocks.sessions
    frames = []
    for window in params.windows:
        rows = window_of(sessions, as_of, window)
        if len(rows) < window or rows[-1] != as_of:
            continue
        names = eligible(stocks, rows, params, window)
        if names.empty:
            continue
        min_obs = params.min_obs(window)
        r = stocks.returns.loc[rows, names].to_numpy(dtype=np.float64)
        e = etf_returns.reindex(index=rows).to_numpy(dtype=np.float64)
        market = (
            etf_returns[params.market].reindex(index=rows).to_numpy(dtype=np.float64)
        )
        raw = pairwise(r, e)
        net = pairwise(
            market_residuals(r, market, min_obs), market_residuals(e, market, min_obs)
        )
        etfs = list(etf_returns.columns)
        is_market = np.array([etf == params.market for etf in etfs])
        beta_net, corr_net = net.beta(), net.corr()
        beta_net[:, is_market] = np.nan
        corr_net[:, is_market] = np.nan
        n_stocks, n_etfs = raw.n.shape
        frame = pd.DataFrame(
            {
                "ticker": np.repeat(np.asarray(names, dtype=object), n_etfs),
                "etf": np.tile(np.asarray(etfs, dtype=object), n_stocks),
                "window": window,
                "n_obs": raw.n.ravel(),
                "beta": raw.beta().ravel(),
                "corr": raw.corr().ravel(),
                "beta_ex_mkt": beta_net.ravel(),
                "corr_ex_mkt": corr_net.ravel(),
            }
        )
        frames.append(frame[(frame["n_obs"] >= min_obs) & frame["beta"].notna()])
    if not frames:
        return SCHEMA.empty_table().to_pandas()
    out = pd.concat(frames, ignore_index=True)
    out.insert(0, "as_of", pd.Timestamp(as_of).date())
    out.insert(3, "etf_group", out["etf"].map(etf_groups).fillna(""))
    for name in ("beta", "corr", "beta_ex_mkt", "corr_ex_mkt"):
        out[name] = out[name].astype(np.float32)
    out["window"] = out["window"].astype(np.int16)
    out["n_obs"] = out["n_obs"].astype(np.int16)
    return out.sort_values(["ticker", "window", "etf"], ignore_index=True)[COLUMNS]
