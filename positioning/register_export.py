"""Inputs for the sized run of the register profit ordering in btest (DD-003 WP21).

EVAL-004 and EVAL-005 found one short-side ordering that reads the same way
in every public register with enough names: the funds' short profit
(SPEC's G7 with identity), beyond reversal and momentum. This module writes
what btest needs to size it, next to WP15's files under
``<positioning root>/exports/btest/``:

- ``register_prices_daily.parquet``: the long daily panel for the issuers
  of the chosen markets (every ISIN that ever had a visible holder), from
  each market's register lane, adjusted, the ticker suffixed with the
  exchange (``SAP.XETRA``, ``AIR.PA``) so one ticker names one listing;
  the pooled dates keep only those on which at least half the median number
  of names have a bar (a market's holiday is not a trading day of the pool).
- ``register_profit.parquet``: a wide frame (``date`` index, one column per
  ticker) with the per-date demeaned rank in [-0.5, 0.5] of the issuer
  panel's ``profit_pct`` residualised on ``reversal_21d`` and
  ``momentum_12_1`` (EVAL-005's ``factors`` cleaning), among the names with
  a visible holder that day. The panel's day ``d`` is marked at ``d``'s
  close, so the value is shown on the next trading day (point in time); a
  name keeps its last value for at most ``CARRY_DAYS`` days after its last
  marked day (a holiday of its own market), then drops out.
  ``register_profit_raw.parquet`` holds the unresidualised rank; the
  ``_neg`` companions are the negations (btest weights each book by a
  positive score). SPEC's orientation: high profit is bearish.
- ``register_borrow_rates.parquet``: ``(snapshot_date, ticker, fee_rate)``
  from the broker's latest country files (``borrow.ib.COUNTRIES``), one
  rate per ISIN, the market's home file first and the lowest fee elsewhere;
  rates as a fraction a year, for the overlay of ``positioning.btest_results``.

The default markets are the euro ones (Germany, France, the Netherlands,
Ireland), so the pooled book is in one currency; Sweden and Norway need a
currency conversion that this cut does not make.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

import pandas as pd

from positioning import evaluation_register as er
from positioning import export
from positioning import register_panel

SUBDIR = export.SUBDIR
PRICES_FILE = "register_prices_daily.parquet"
SIGNAL_FILE = "register_profit.parquet"
SIGNAL_RAW_FILE = "register_profit_raw.parquet"
BORROW_FILE = "register_borrow_rates.parquet"
EUR_MARKETS: tuple[str, ...] = ("de", "fr", "nl", "ie")
#: EODHD's exchange suffix per market, appended to the code so one ticker names one listing.
SUFFIX: dict[str, str] = {"de": "XETRA", "fr": "PA", "nl": "AS", "ie": "IR", "uk": "LSE", "se": "ST", "no": "OL"}
#: The broker's home file per market, taken first when an ISIN appears in several files.
HOME_FILE: dict[str, str] = {"de": "germany", "fr": "france", "nl": "dutch", "ie": "british", "uk": "british", "se": "swedish"}
FACTORS: tuple[str, ...] = ("reversal_21d", "momentum_12_1")
DEFAULT_START = "2013-01-01"
CARRY_DAYS = 3


@dataclass(frozen=True)
class RegisterExportReport:
    directory: Path
    markets: tuple[str, ...]
    symbols: int
    price_rows: int
    first_date: str
    last_date: str
    signal_dates: int
    signal_symbols: int
    borrow_symbols: int
    borrow_snapshot: str | None


def load_panels(root: Path, markets: Sequence[str]) -> pd.DataFrame:
    """The markets' issuer panels concatenated, the ticker suffixed with the exchange."""
    frames = []
    for market in markets:
        path = Path(root) / register_panel.SUBDIR / market / "issuers.parquet"
        frame = pd.read_parquet(path)
        frame["date"] = pd.to_datetime(frame["date"])
        frame["code"] = frame["ticker"]
        frame["ticker"] = frame["ticker"] + "." + SUFFIX[market]
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


def price_panel(con: Any, panels: pd.DataFrame, *, start: str) -> pd.DataFrame:
    """btest's long daily panel over the issuers of each market's lane, pooled and suffixed."""
    frames = []
    for market, group in panels.groupby("market", sort=True):
        lane, _ = register_panel.MARKET_LANES[market]
        codes = sorted(group["code"].unique())
        frame = export.price_panel(con, codes, start=start, lanes=(lane,))
        frame["ticker"] = frame["ticker"] + "." + SUFFIX[market]
        frames.append(frame)
    pooled = pd.concat(frames, ignore_index=True)
    pooled["date"] = pooled["date"].dt.tz_localize(None)
    pooled = export.trading_days(pooled).sort_values(["ticker", "date"]).reset_index(drop=True)
    pooled["date"] = pooled["date"].dt.tz_localize("UTC")
    return pooled


def signal_observations(panels: pd.DataFrame, *, factors: Sequence[str] = FACTORS, min_names: int = er.MIN_NAMES) -> pd.DataFrame:
    """``(ticker, date, profit, profit_raw)`` per marked day: demeaned ranks among the names with a holder."""
    frame = panels[(panels["n_holders"] > 0) & panels["profit_pct"].notna()].copy()
    frame["resid"] = er.residualise(frame, "profit_pct", tuple(factors), min_names=min_names)
    frame["profit"] = export.demeaned_rank(frame, "resid")
    frame["profit_raw"] = export.demeaned_rank(frame, "profit_pct")
    return frame[["ticker", "date", "profit", "profit_raw"]].reset_index(drop=True)


def daily_wide(observations: pd.DataFrame, value: str, *, dates: Sequence[Any], carry_days: int = CARRY_DAYS) -> pd.DataFrame:
    """Pivot the marked days to the trading index, shown from the next trading day, carried at most ``carry_days``."""
    index = pd.DatetimeIndex(pd.to_datetime(list(dates))).sort_values().unique()
    wide = observations.pivot(index="date", columns="ticker", values=value).sort_index()
    wide = wide.reindex(index.union(wide.index)).ffill(limit=carry_days).reindex(index)
    return wide.shift(1)


def borrow_rates(borrow_root: Path, panels: pd.DataFrame) -> pd.DataFrame:
    """One rate per issuer from the broker's latest country files: the home file first, else the lowest fee."""
    from borrow import ib

    mapping = panels[["market", "isin", "ticker"]].drop_duplicates(["isin", "ticker"])
    snapshots: dict[str, pd.DataFrame] = {}
    for country in ib.COUNTRIES:
        if country == "usa":
            continue
        target = ib.store(borrow_root, country)
        days = target.days_on_disk()
        if not days:
            continue
        frame = target.read_day(days[-1])
        if frame is None:
            continue
        frame = frame[frame["isin"].notna() & frame["fee_rate"].notna()]
        snapshots[country] = frame.sort_values("fee_rate").drop_duplicates("isin")[["snapshot_date", "isin", "fee_rate"]]
    rows = []
    for market, group in mapping.groupby("market", sort=True):
        home = HOME_FILE.get(market)
        pending = group
        for country, frame in [(home, snapshots.get(home))] + [(c, f) for c, f in snapshots.items() if c != home]:
            if frame is None or pending.empty:
                continue
            hit = pending.merge(frame, on="isin", how="inner")
            rows.append(hit)
            pending = pending[~pending["isin"].isin(set(hit["isin"]))]
    if not rows:
        return pd.DataFrame(columns=["snapshot_date", "ticker", "fee_rate"])
    out = pd.concat(rows, ignore_index=True)
    out = out.sort_values("fee_rate").drop_duplicates("ticker")
    out["fee_rate"] = out["fee_rate"].astype(float) / 100.0
    return out[["snapshot_date", "ticker", "fee_rate"]].sort_values("ticker").reset_index(drop=True)


def export_registers(
    con: Any,
    root: Path,
    *,
    markets: Sequence[str] = EUR_MARKETS,
    start: str = DEFAULT_START,
    borrow_root: Path | None = None,
) -> RegisterExportReport:
    directory = Path(root) / SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    panels = load_panels(root, markets)
    panels = panels[panels["date"] >= pd.Timestamp(start)]
    prices = price_panel(con, panels, start=start)
    prices.to_parquet(directory / PRICES_FILE, index=False)
    dates = prices["date"].dt.tz_localize(None).drop_duplicates().sort_values()
    observations = signal_observations(panels)
    priced = set(prices["ticker"].unique())
    observations = observations[observations["ticker"].isin(priced)]
    signal = daily_wide(observations, "profit", dates=dates)
    export.write_pair(signal, directory, SIGNAL_FILE)
    export.write_pair(daily_wide(observations, "profit_raw", dates=dates), directory, SIGNAL_RAW_FILE)
    borrow = pd.DataFrame(columns=["snapshot_date", "ticker", "fee_rate"])
    if borrow_root is not None:
        borrow = borrow_rates(borrow_root, panels[panels["ticker"].isin(priced)])
        borrow.to_parquet(directory / BORROW_FILE, index=False)
    return RegisterExportReport(
        directory=directory,
        markets=tuple(markets),
        symbols=len(priced),
        price_rows=int(len(prices)),
        first_date=str(dates.min().date()),
        last_date=str(dates.max().date()),
        signal_dates=int(signal.notna().any(axis=1).sum()),
        signal_symbols=int(signal.notna().any().sum()),
        borrow_symbols=int(len(borrow)),
        borrow_snapshot=str(pd.to_datetime(borrow["snapshot_date"]).max().date()) if len(borrow) else None,
    )
