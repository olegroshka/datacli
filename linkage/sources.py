"""The inputs of the linkage datasets: small interfaces and their adapters.

The builders depend on these protocols only; tests hand them in-memory fakes.

- :class:`PriceSource` -- daily closes, volume, split days and dividends
  (adapter: ``daily_store.DailyStore`` over the eodhd US lanes, which already
  has this shape);
- :class:`UniverseSource` -- the context ETFs and the point-in-time
  membership of the intraday lane (adapter: :class:`IntradayFiles`);
- :class:`AuctionSource` -- one row per ticker and day with the opening and
  closing cross (adapter: :class:`AlpacaAuctions`).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Iterable, Protocol, Sequence

import pandas as pd

STOCK, ETF = "stock", "etf"
CONTEXT_TIER = "context"
STOCK_TIERS = ("T1", "T2")


class PriceSource(Protocol):
    def prices(
        self,
        start: dt.date,
        end: dt.date,
        *,
        tickers: Sequence[str] | None = None,
        asset_types: Iterable[str] | None = None,
        columns: Sequence[str] = ("close", "volume"),
    ) -> pd.DataFrame:
        """``ticker, asset_type, date (datetime64), <columns>``."""
        ...

    def split_days(
        self, start: dt.date, end: dt.date, *, tickers: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """``ticker, ex_date (datetime64), ratio``."""
        ...

    def dividends(
        self, start: dt.date, end: dt.date, *, tickers: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """``ticker, ex_date (datetime64), amount``."""
        ...


class UniverseSource(Protocol):
    def context_etfs(self) -> pd.DataFrame:
        """``ticker, group``: the context ETFs."""
        ...

    def membership(self) -> pd.DataFrame:
        """``quarter_start (datetime64), ticker, asset_type, tier``: US stocks and ETFs per quarter."""
        ...


class AuctionSource(Protocol):
    def daily(self) -> pd.DataFrame:
        """``ticker, date (datetime64), open_price, open_size, close_price, close_size``."""
        ...


class IntradayFiles:
    """The eodhd intraday lane's universe files (read-only)."""

    def __init__(self, eodhd_root: Path) -> None:
        self.dir = Path(eodhd_root) / "intraday"

    def _membership_raw(self) -> pd.DataFrame:
        path = self.dir / "universe_membership.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no intraday membership file at {path}")
        return pd.read_parquet(
            path,
            columns=[
                "quarter_start",
                "ticker",
                "exchange",
                "tier",
                "asset_type",
                "group",
            ],
        )

    def context_etfs(self) -> pd.DataFrame:
        raw = self._membership_raw()
        rows = raw[(raw["tier"] == CONTEXT_TIER) & (raw["exchange"] == "US")]
        return (
            rows[["ticker", "group"]]
            .drop_duplicates("ticker")
            .sort_values("ticker", ignore_index=True)
        )

    def membership(self) -> pd.DataFrame:
        raw = self._membership_raw()
        rows = raw[(raw["exchange"] == "US") & raw["asset_type"].isin((STOCK, ETF))]
        out = rows[["quarter_start", "ticker", "asset_type", "tier"]].copy()
        out["quarter_start"] = pd.to_datetime(out["quarter_start"])
        return out.reset_index(drop=True)


class AlpacaAuctions:
    """The Alpaca auctions daily file (read-only)."""

    COLUMNS = ["ticker", "date", "open_price", "open_size", "close_price", "close_size"]

    def __init__(self, alpaca_root: Path) -> None:
        self.path = Path(alpaca_root) / "auctions" / "auctions_daily.parquet"

    def daily(self) -> pd.DataFrame:
        if not self.path.exists():
            raise FileNotFoundError(
                f"no auctions at {self.path}; run: alpaca fetch --run"
            )
        frame = pd.read_parquet(self.path, columns=self.COLUMNS)
        frame["date"] = pd.to_datetime(frame["date"])
        return frame
