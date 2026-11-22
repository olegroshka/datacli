"""One month-end snapshot of auction co-spike neighbours. Pure.

Input: the ``resid`` of :mod:`linkage.abnormal` for one side, as a wide panel
(auction days x tickers), and the names that are members at ``as_of``. Over
the last ``window`` auction days up to and including ``as_of``:

- ``corr``: Pearson correlation of two names' residuals (clipped to
  ``+-clip``) over the days both have one (``n_overlap``, at least
  ``min_overlap``);
- ``co_spikes``: days both residuals exceed ``spike_threshold`` (both names'
  auctions unusually large, net of the day); ``expected_co_spikes`` is what
  independence would give over the same days, and ``lift`` the ratio.

Each name keeps its ``top_k`` neighbours by ``corr`` (rank 1 = closest). The
lists are directed: B can be in A's list without A being in B's.
"""

from __future__ import annotations

from typing import Mapping, Sequence

import numpy as np
import pandas as pd
import pyarrow as pa

from linkage.calendar import window as window_of
from linkage.config import CospikeParams
from linkage.moments import pairwise

SCHEMA = pa.schema(
    [
        ("as_of", pa.date32()),
        ("ticker", pa.string()),
        ("neighbor", pa.string()),
        ("rank", pa.int16()),
        ("corr", pa.float32()),
        ("n_overlap", pa.int16()),
        ("co_spikes", pa.int16()),
        ("expected_co_spikes", pa.float32()),
        ("lift", pa.float32()),
        ("asset_type", pa.string()),
        ("neighbor_asset_type", pa.string()),
    ]
)
COLUMNS = list(SCHEMA.names)


def snapshot(
    resid: pd.DataFrame,
    members: Sequence[str],
    asset_types: Mapping[str, str],
    as_of: pd.Timestamp,
    params: CospikeParams,
) -> pd.DataFrame:
    days = window_of(pd.DatetimeIndex(resid.index), as_of, params.window)
    names = [m for m in members if m in resid.columns]
    if len(days) < params.min_overlap or len(names) < 2:
        return SCHEMA.empty_table().to_pandas()
    z = np.clip(
        resid.loc[days, names].to_numpy(dtype=np.float64), -params.clip, params.clip
    )
    moments = pairwise(z, z)
    corr, n = moments.corr(), moments.n
    valid = (~np.isnan(z)).astype(np.float64)
    spikes = np.where(np.isnan(z), 0.0, (z > params.spike_threshold).astype(np.float64))
    co = spikes.T @ spikes
    own = spikes.T @ valid  # own[i, j]: i's spikes on days j has a value
    with np.errstate(divide="ignore", invalid="ignore"):
        expected = own * own.T / n
        lift = np.where(expected > 0, co / expected, np.nan)
    np.fill_diagonal(corr, np.nan)
    corr[n < params.min_overlap] = np.nan
    # highest correlation first; missing ones (the diagonal, too little overlap) last
    order = np.argsort(np.where(np.isnan(corr), np.inf, -corr), axis=1, kind="stable")
    rows = []
    for i, ticker in enumerate(names):
        picked = [j for j in order[i, : params.top_k] if not np.isnan(corr[i, j])]
        for rank, j in enumerate(picked, start=1):
            rows.append(
                (
                    ticker,
                    names[j],
                    rank,
                    corr[i, j],
                    n[i, j],
                    co[i, j],
                    expected[i, j],
                    lift[i, j],
                )
            )
    if not rows:
        return SCHEMA.empty_table().to_pandas()
    out = pd.DataFrame(
        rows,
        columns=[
            "ticker",
            "neighbor",
            "rank",
            "corr",
            "n_overlap",
            "co_spikes",
            "expected_co_spikes",
            "lift",
        ],
    )
    out.insert(0, "as_of", pd.Timestamp(as_of).date())
    out["asset_type"] = out["ticker"].map(asset_types).fillna("")
    out["neighbor_asset_type"] = out["neighbor"].map(asset_types).fillna("")
    out = out.astype(
        {
            "rank": np.int16,
            "corr": np.float32,
            "n_overlap": np.int16,
            "co_spikes": np.int16,
            "expected_co_spikes": np.float32,
            "lift": np.float32,
        }
    )
    return out[COLUMNS]
