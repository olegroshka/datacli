"""Load one market's register and prices, build the per-fund ladders and the issuer panel, store them.

The derived files live under ``<positioning root>/registers/<market>/``:
``funds.parquet`` (one row per visible register row with its ladder state,
:data:`positioning.register_ladder.FUND_COLUMNS`) and ``issuers.parquet``
(the daily point-in-time issuer panel, :data:`PANEL_COLUMNS`, plus the
``ticker`` the ISIN is priced as and the price factors ``reversal_21d`` and
``momentum_12_1`` computed from the lane's adjusted closes). A first cut
written by hand (DD-003 WP18); not yet a scheduled dataset.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from positioning import register_ladder as rl

SUBDIR = Path("registers")
#: Which EODHD lane prices each market, and the universe parquet that maps ISIN to code.
MARKET_LANES: dict[str, tuple[str, str]] = {
    "uk": ("uk_domestic", "tickers_UK.parquet"),
    "fr": ("fr_domestic", "tickers_FR.parquet"),
    "nl": ("nl_domestic", "tickers_NL.parquet"),
    "se": ("se_domestic", "tickers_SE.parquet"),
    "no": ("no_domestic", "tickers_NO.parquet"),
    "ie": ("ie_domestic", "tickers_IE.parquet"),
    "de": ("de_domestic", "tickers_DE.parquet"),
}
MIN_CLOSE = 0.0001
MAX_CLOSE = 999999.0


@dataclass(frozen=True)
class PanelReport:
    market: str
    directory: Path
    register_rows: int
    pairs: int
    pairs_priced: int
    fund_rows: int
    issuers: int
    panel_rows: int
    first_date: str
    last_date: str


def isin_to_ticker(con: Any, universe_path: Path) -> dict[str, str]:
    """ISIN to provider code from the lane's universe file (the first code per ISIN, register names first)."""
    frame = con.execute(f"SELECT Code AS ticker, upper(Isin) AS isin FROM read_parquet('{universe_path.as_posix()}') WHERE Isin IS NOT NULL AND Isin <> ''").df()
    return dict(zip(frame["isin"], frame["ticker"]))


def closes_by_ticker(con: Any, lane: str, tickers: list[str]) -> dict[str, pd.Series]:
    """Raw closes (the register's percent needs no split handling, the lot price is the day's close) per ticker."""
    con.register("_register_tickers", pd.DataFrame({"ticker": tickers}))
    try:
        frame = con.execute(f"""
            SELECT ticker, CAST(date AS DATE) AS date, close, adjusted_close
            FROM prices WHERE lane = '{lane}' AND close > {MIN_CLOSE} AND close < {MAX_CLOSE}
              AND ticker IN (SELECT ticker FROM _register_tickers)
            ORDER BY ticker, date
            """).df()
    finally:
        con.unregister("_register_tickers")
    frame["date"] = pd.to_datetime(frame["date"])
    out: dict[str, pd.Series] = {}
    adjusted: dict[str, pd.Series] = {}
    for ticker, g in frame.groupby("ticker", sort=False):
        out[ticker] = pd.Series(g["close"].to_numpy(dtype=float), index=pd.DatetimeIndex(g["date"]))
        adjusted[ticker] = pd.Series(g["adjusted_close"].to_numpy(dtype=float), index=pd.DatetimeIndex(g["date"]))
    out["__adjusted__"] = adjusted  # type: ignore[assignment]
    return out


def price_factors(adjusted: pd.Series, dates: pd.DatetimeIndex) -> pd.DataFrame:
    """``reversal_21d`` (21-day return) and ``momentum_12_1`` (252-day return skipping the last 21) on the trading-day index."""
    s = adjusted.reindex(dates).ffill()
    rev = s / s.shift(21) - 1.0
    mom = s.shift(21) / s.shift(252) - 1.0
    return pd.DataFrame({"reversal_21d": rev, "momentum_12_1": mom})


def build(con: Any, root: Path, market: str) -> PanelReport:
    lane, universe_file = MARKET_LANES[market]
    import _datadir  # type: ignore[import-not-found]

    universe_path = _datadir.EODHD_RAW_ROOT / lane / universe_file
    register = con.execute(f"SELECT * FROM short_register WHERE market = '{market}'").df()
    rows = rl.prepare_rows(register)
    mapping = isin_to_ticker(con, universe_path)
    rows["ticker"] = rows["isin"].str.upper().map(mapping)
    priced_isins = sorted(set(rows.loc[rows["ticker"].notna(), "isin"]))
    tickers = sorted({mapping[i.upper()] for i in priced_isins})
    closes = closes_by_ticker(con, lane, tickers)
    adjusted = closes.pop("__adjusted__")  # type: ignore[arg-type]
    closes_by_isin = {isin: closes[mapping[isin.upper()]] for isin in priced_isins if mapping[isin.upper()] in closes}
    adjusted_by_isin = {isin: adjusted[mapping[isin.upper()]] for isin in priced_isins if mapping[isin.upper()] in adjusted}  # type: ignore[index]
    funds = rl.fund_ladders(rows, closes_by_isin)
    dates = pd.DatetimeIndex(sorted({d for s in closes_by_isin.values() for d in s.index}))
    panel = rl.issuer_panel(funds, dates, closes_by_isin)
    if not panel.empty:
        panel["ticker"] = panel["isin"].str.upper().map(mapping)
        factors = []
        for isin, g in panel.groupby("isin", sort=False):
            days = pd.DatetimeIndex(g["date"])
            f = price_factors(adjusted_by_isin[isin], days)
            f["adjusted_close"] = adjusted_by_isin[isin].reindex(days).ffill().to_numpy()
            f.index = g.index
            factors.append(f)
        panel = pd.concat([panel, pd.concat(factors)], axis=1)
    directory = Path(root) / SUBDIR / market
    directory.mkdir(parents=True, exist_ok=True)
    funds.to_parquet(directory / "funds.parquet", index=False)
    panel.to_parquet(directory / "issuers.parquet", index=False)
    pairs = rows.groupby(["holder", "isin"]).ngroups
    return PanelReport(
        market=market, directory=directory, register_rows=len(rows), pairs=pairs,
        pairs_priced=funds.groupby(["holder", "isin"]).ngroups if not funds.empty else 0,
        fund_rows=len(funds), issuers=int(panel["isin"].nunique()) if not panel.empty else 0, panel_rows=len(panel),
        first_date=str(panel["date"].min().date()) if not panel.empty else "", last_date=str(panel["date"].max().date()) if not panel.empty else "",
    )
