"""Read-only access to the US daily lanes: prices, splits and dividends.

One adapter over the files the eodhd fetchers write, for the sources that
derive from them (``alpaca`` reads symbol spans, ``linkage`` reads closes and
corporate events)::

    <eodhd_root>/<lane>/prices_daily.parquet
    <eodhd_root>/<lane>/splits_history.parquet
    <eodhd_root>/<lane>/dividends_history.parquet

Stocks come from ``us_common`` then ``us_extended``, ETFs from ``us_etf``. A
ticker present in more than one lane is read from the first lane that has it,
so a caller never sees two series under one name.

What the files hold (see ``docs/INTRADAY_1M_DESIGN.md`` section 4): ``close``
is the price as traded (raw) except for serial reverse-splitters in
``us_extended`` whose history is already adjusted; ``volume`` is restated for
later splits; ``adjusted_close`` is not re-adjusted by the bulk top-up. A
consumer that needs returns should therefore not trust either convention
across a corporate event: it should drop event days (``linkage.returns``).
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any, Iterable, Sequence

import pandas as pd

STOCK = "stock"
ETF = "etf"
LANES: tuple[tuple[str, str], ...] = (
    ("us_common", STOCK),
    ("us_extended", STOCK),
    ("us_etf", ETF),
)
PRICES, SPLITS, DIVIDENDS = "prices_daily", "splits_history", "dividends_history"


class DailyStore:
    """The US daily lanes under one eodhd root (DuckDB over parquet, read-only)."""

    def __init__(self, root: Path, *, lanes: Sequence[tuple[str, str]] = LANES) -> None:
        self.root = Path(root)
        self.lanes = tuple(lanes)

    # ----- paths ----------------------------------------------------------- #
    def path(self, lane: str, kind: str) -> Path:
        return self.root / lane / f"{kind}.parquet"

    def _lanes(
        self, asset_types: Iterable[str] | None, kind: str
    ) -> list[tuple[str, str]]:
        wanted = None if asset_types is None else set(asset_types)
        return [
            (lane, asset)
            for lane, asset in self.lanes
            if (wanted is None or asset in wanted) and self.path(lane, kind).exists()
        ]

    def _ranked(
        self, kind: str, asset_types: Iterable[str] | None, select: str, where: str
    ) -> str:
        """A UNION over the lanes with a ``lane_rank`` column (lower wins)."""
        parts = [
            f"SELECT {select}, {rank} AS lane_rank, '{asset}' AS asset_type "
            f"FROM read_parquet('{self.path(lane, kind).as_posix()}') WHERE {where}"
            for rank, (lane, asset) in enumerate(self._lanes(asset_types, kind))
        ]
        if not parts:
            return ""
        return " UNION ALL ".join(parts)

    @staticmethod
    def _connect() -> Any:
        import duckdb

        return duckdb.connect()

    # ----- spans ----------------------------------------------------------- #
    def spans(
        self,
        tickers: Sequence[str] | None = None,
        *,
        asset_types: Iterable[str] | None = None,
    ) -> pd.DataFrame:
        """``ticker, asset_type, first, last`` (dates) of each ticker's daily prices."""
        con = self._connect()
        where = "ticker = ANY($tickers)" if tickers is not None else "TRUE"
        union = self._ranked(
            PRICES, asset_types, "ticker, CAST(date AS DATE) AS date", where
        )
        if not union:
            return pd.DataFrame(columns=["ticker", "asset_type", "first", "last"])
        sql = (
            "WITH u AS ("
            + union
            + "), owner AS (SELECT ticker, min(lane_rank) AS lane_rank FROM u GROUP BY ticker) "
            "SELECT u.ticker, any_value(u.asset_type) AS asset_type, min(u.date) AS first, max(u.date) AS last "
            "FROM u JOIN owner USING (ticker, lane_rank) GROUP BY u.ticker ORDER BY u.ticker"
        )
        params = {"tickers": list(tickers)} if tickers is not None else {}
        frame = con.execute(sql, params).df()
        for col in ("first", "last"):
            frame[col] = pd.to_datetime(frame[col]).dt.date
        return frame

    # ----- prices ---------------------------------------------------------- #
    def prices(
        self,
        start: dt.date,
        end: dt.date,
        *,
        tickers: Sequence[str] | None = None,
        asset_types: Iterable[str] | None = None,
        columns: Sequence[str] = ("close", "volume"),
    ) -> pd.DataFrame:
        """``ticker, asset_type, date, <columns>`` in ``[start, end]``, one lane per ticker.

        ``columns`` is any subset of ``open, high, low, close, adjusted_close,
        volume``; rows need a positive close.
        """
        allowed = {"open", "high", "low", "close", "adjusted_close", "volume"}
        bad = [c for c in columns if c not in allowed]
        if bad:
            raise ValueError(f"unknown price column(s): {bad}")
        cols = ", ".join(columns)
        con = self._connect()
        where = "CAST(date AS DATE) BETWEEN $start AND $end AND close > 0"
        if tickers is not None:
            where += " AND ticker = ANY($tickers)"
        select = "ticker, CAST(date AS DATE) AS date, close" + "".join(
            f", {c}" for c in columns if c != "close"
        )
        union = self._ranked(PRICES, asset_types, select, where)
        if not union:
            return pd.DataFrame(columns=["ticker", "asset_type", "date", *columns])
        sql = (
            "WITH u AS ("
            + union
            + "), owner AS (SELECT ticker, min(lane_rank) AS lane_rank FROM u GROUP BY ticker) "
            f"SELECT ticker, asset_type, date, {cols} FROM u JOIN owner USING (ticker, lane_rank) "
            "QUALIFY row_number() OVER (PARTITION BY ticker, date ORDER BY close) = 1 "
            "ORDER BY ticker, date"
        )
        params: dict[str, Any] = {"start": start, "end": end}
        if tickers is not None:
            params["tickers"] = list(tickers)
        frame = con.execute(sql, params).df()
        frame["date"] = pd.to_datetime(frame["date"])
        return frame

    def liquid_tickers(
        self,
        start: dt.date,
        end: dt.date,
        *,
        min_price: float,
        min_dollar_volume: float,
        asset_types: Iterable[str] | None = None,
    ) -> list[str]:
        """Tickers with at least one calendar month in ``[start, end]`` whose median
        close and median close * volume clear the floors.

        A coarse screen to keep a load small, not a point-in-time filter:
        callers apply their own rule per date on what this returns.
        """
        con = self._connect()
        where = "CAST(date AS DATE) BETWEEN $start AND $end AND close > 0"
        union = self._ranked(
            PRICES,
            asset_types,
            "ticker, CAST(date AS DATE) AS date, close, volume",
            where,
        )
        if not union:
            return []
        sql = (
            "WITH u AS ("
            + union
            + "), owner AS (SELECT ticker, min(lane_rank) AS lane_rank FROM u GROUP BY ticker), "
            "m AS (SELECT ticker, date_trunc('month', date) AS month, median(close) AS px, "
            "median(close * volume) AS dv FROM u JOIN owner USING (ticker, lane_rank) GROUP BY ticker, month) "
            "SELECT DISTINCT ticker FROM m WHERE px >= $min_price AND dv >= $min_dv ORDER BY ticker"
        )
        rows = con.execute(
            sql,
            {
                "start": start,
                "end": end,
                "min_price": min_price,
                "min_dv": min_dollar_volume,
            },
        ).fetchall()
        return [r[0] for r in rows]

    # ----- events ---------------------------------------------------------- #
    def split_days(
        self, start: dt.date, end: dt.date, *, tickers: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """``ticker, ex_date, ratio`` for every split with a ratio other than 1."""
        return self._events(
            SPLITS,
            "ticker, TRY_CAST(ex_date AS DATE) AS ex_date, "
            "CASE WHEN denominator > 0 THEN numerator / denominator END AS value",
            start,
            end,
            tickers,
            "ratio",
            keep="value IS NULL OR abs(value - 1) > 1e-9",
        )

    def dividends(
        self, start: dt.date, end: dt.date, *, tickers: Sequence[str] | None = None
    ) -> pd.DataFrame:
        """``ticker, ex_date, amount``: the dividend as paid per share on its ex-date."""
        return self._events(
            DIVIDENDS,
            "ticker, TRY_CAST(ex_date AS DATE) AS ex_date, coalesce(unadjusted_dividend, dividend) AS value",
            start,
            end,
            tickers,
            "amount",
            keep="value > 0",
        )

    def _events(
        self,
        kind: str,
        select: str,
        start: dt.date,
        end: dt.date,
        tickers: Sequence[str] | None,
        name: str,
        *,
        keep: str,
    ) -> pd.DataFrame:
        con = self._connect()
        where = "TRY_CAST(ex_date AS DATE) BETWEEN $start AND $end"
        if tickers is not None:
            where += " AND ticker = ANY($tickers)"
        union = self._ranked(kind, None, select, where)
        if not union:
            return pd.DataFrame(columns=["ticker", "ex_date", name])
        # Events of a ticker apply whichever lane recorded them; two lanes that
        # recorded the same event once each must not count it twice.
        sql = (
            f"SELECT ticker, ex_date, max(value) AS {name} FROM ({union}) WHERE {keep} "
            "GROUP BY ticker, ex_date ORDER BY ticker, ex_date"
        )
        params: dict[str, Any] = {"start": start, "end": end}
        if tickers is not None:
            params["tickers"] = list(tickers)
        frame = con.execute(sql, params).df()
        frame["ex_date"] = pd.to_datetime(frame["ex_date"])
        return frame
