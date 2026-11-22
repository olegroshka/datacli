"""Unusual auction value per name and day. Pure.

For each side (closing and opening cross), name and day:

- ``value_usd`` = cross price * cross size. Dollar value, not shares: sizes
  are shares as traded, so a split would otherwise look like a spike for
  weeks;
- ``baseline`` = the median ``log(value_usd)`` of the name's previous
  ``baseline_window`` auction days (today excluded, at least
  ``baseline_min_obs`` of them);
- ``abnormal`` = ``log(value_usd) - baseline``;
- ``market_abnormal`` = the median ``abnormal`` that day over the members of
  the intraday lane of the same asset type (stocks, ETFs);
- ``resid`` = ``abnormal - market_abnormal``. Quarterly expiry, index
  rebalance and month-end days lift every name's auction at once (the median
  closing cross is about 24.5% of the day's volume on quarterly expiries
  against 8.4% otherwise); netting the day out leaves what is particular to
  the name.

Everything at day t uses auctions dated on or before t; the cross-section of
day t is known after that day's close.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pyarrow as pa

from linkage.config import AbnormalParams

SCHEMA = pa.schema(
    [
        ("date", pa.date32()),
        ("ticker", pa.string()),
        ("side", pa.string()),
        ("asset_type", pa.string()),
        ("is_member", pa.bool_()),
        ("value_usd", pa.float64()),
        ("log_value", pa.float32()),
        ("baseline", pa.float32()),
        ("abnormal", pa.float32()),
        ("market_abnormal", pa.float32()),
        ("resid", pa.float32()),
    ]
)
COLUMNS = list(SCHEMA.names)


def member_flags(frame: pd.DataFrame, membership: pd.DataFrame) -> pd.Series:
    """Whether each ``(ticker, date)`` row's ticker is a member in the date's quarter."""
    quarters = frame["date"].dt.to_period("Q").dt.start_time
    keys = pd.MultiIndex.from_arrays([quarters, frame["ticker"]])
    members = pd.MultiIndex.from_frame(
        membership[["quarter_start", "ticker"]].drop_duplicates()
    )
    return pd.Series(keys.isin(members), index=frame.index)


def build(
    auctions: pd.DataFrame, membership: pd.DataFrame, params: AbnormalParams
) -> pd.DataFrame:
    """The long panel over every side in ``params.sides``."""
    types = membership.drop_duplicates("ticker").set_index("ticker")["asset_type"]
    frames = []
    for side in params.sides:
        part = auctions.loc[:, ["ticker", "date"]].copy()
        part["value_usd"] = auctions[f"{side}_price"].astype(float) * auctions[
            f"{side}_size"
        ].astype(float)
        part = part[part["value_usd"] > 0].sort_values(
            ["ticker", "date"], ignore_index=True
        )
        if part.empty:
            continue
        part["side"] = side
        part["log_value"] = np.log(part["value_usd"])
        part["baseline"] = part.groupby("ticker", sort=False)["log_value"].transform(
            lambda s: s.shift(1)
            .rolling(params.baseline_window, min_periods=params.baseline_min_obs)
            .median()
        )
        part["abnormal"] = part["log_value"] - part["baseline"]
        part["asset_type"] = part["ticker"].map(types).fillna("")
        part["is_member"] = member_flags(part, membership).to_numpy()
        peers = part[part["is_member"] & part["abnormal"].notna()]
        day_median = (
            peers.groupby(["date", "asset_type"])["abnormal"]
            .median()
            .rename("market_abnormal")
        )
        part = part.join(day_median, on=["date", "asset_type"])
        part["resid"] = part["abnormal"] - part["market_abnormal"]
        frames.append(part)
    if not frames:
        return SCHEMA.empty_table().to_pandas()
    out = pd.concat(frames, ignore_index=True)
    out["date"] = out["date"].dt.date
    for name in ("log_value", "baseline", "abnormal", "market_abnormal", "resid"):
        out[name] = out[name].astype(np.float32)
    return out.sort_values(["date", "side", "ticker"], ignore_index=True)[COLUMNS]
