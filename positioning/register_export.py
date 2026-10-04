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
  One snapshot stands for the whole history (today's rates applied
  backwards), which breaks on a name that is squeezed today and was not
  years ago (a rate of several hundred percent on a one percent short
  weight is most of a book's cost); ``rate_cap`` also writes
  ``register_borrow_rates_cap<pct>.parquet`` with the fees clipped at the
  cap, the recorded assumption for a backward application.

The default markets are the euro ones (Germany, France, the Netherlands,
Ireland), so the pooled book is in one currency; Sweden and Norway need a
currency conversion that this cut does not make. The UK (``--markets uk``,
written under ``exports/btest_uk/``) is one currency too once the pence
quotes are scaled to pounds (``QUOTE_SCALE``): the vendor quotes most LSE
names in GBX, a few in GBP, and a handful of the register's issuers in
euros or dollars, which are dropped so that a book, a price floor and a
volume floor mean one thing.
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
#: Per market, the factor that takes each quote currency of its lane to the book's currency; other currencies are dropped.
QUOTE_SCALE: dict[str, dict[str, float]] = {"uk": {"GBX": 0.01, "GBP": 1.0}}


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
    borrow_capped: int = 0


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


def quote_scale(universe: pd.DataFrame, scale: dict[str, float]) -> pd.Series:
    """Per code, the factor that takes the lane's quote currency to the book's; codes in other currencies are left out."""
    currency = universe["Currency"].astype(str).str.strip().str.upper()
    factors = currency.map(scale)
    return pd.Series(factors.values, index=universe["Code"].astype(str).values, dtype="float64").dropna()


def lane_quote_scale(market: str) -> pd.Series | None:
    """``quote_scale`` from the market's lane universe file, or None when the market's quotes need no scaling."""
    scale = QUOTE_SCALE.get(market)
    if scale is None:
        return None
    import _datadir  # type: ignore[import-not-found]

    lane, universe_file = register_panel.MARKET_LANES[market]
    universe = pd.read_parquet(_datadir.EODHD_RAW_ROOT / lane / universe_file, columns=["Code", "Currency"])
    return quote_scale(universe, scale)


def scale_quotes(frame: pd.DataFrame, factors: pd.Series) -> pd.DataFrame:
    """Multiply the price columns by the code's factor; drop the codes without one (volume is in shares and stays)."""
    factor = frame["ticker"].map(factors)
    out = frame[factor.notna()].copy()
    factor = factor[factor.notna()]
    for column in ("close", "open", "high", "low"):
        out[column] = out[column] * factor
    return out


def price_panel(con: Any, panels: pd.DataFrame, *, start: str, scales: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """btest's long daily panel over the issuers of each market's lane, pooled, suffixed, in the book's currency."""
    frames = []
    for market, group in panels.groupby("market", sort=True):
        lane, _ = register_panel.MARKET_LANES[market]
        codes = sorted(group["code"].unique())
        frame = export.price_panel(con, codes, start=start, lanes=(lane,))
        factors = (scales or {}).get(market)
        if factors is not None:
            frame = scale_quotes(frame, factors)
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


def capped_rates_file(cap: float) -> str:
    """``register_borrow_rates_cap50.parquet`` for a cap of 0.5 a year."""
    return f"{Path(BORROW_FILE).stem}_cap{int(round(cap * 100))}.parquet"


def cap_rates(borrow: pd.DataFrame, cap: float) -> pd.DataFrame:
    """The rates frame with ``fee_rate`` clipped at ``cap`` (a fraction a year)."""
    if cap <= 0:
        raise ValueError("the rate cap must be a positive fraction a year")
    out = borrow.copy()
    out["fee_rate"] = out["fee_rate"].astype(float).clip(upper=cap)
    return out


def export_subdir(markets: Sequence[str]) -> Path:
    """The euro default writes next to WP15's files; any other set of markets gets its own folder."""
    return SUBDIR if tuple(markets) == EUR_MARKETS else SUBDIR.with_name(f"{SUBDIR.name}_{'_'.join(markets)}")


def export_registers(
    con: Any,
    root: Path,
    *,
    markets: Sequence[str] = EUR_MARKETS,
    start: str = DEFAULT_START,
    borrow_root: Path | None = None,
    subdir: Path | None = None,
    rate_cap: float | None = None,
) -> RegisterExportReport:
    directory = Path(root) / (subdir or export_subdir(markets))
    directory.mkdir(parents=True, exist_ok=True)
    panels = load_panels(root, markets)
    panels = panels[panels["date"] >= pd.Timestamp(start)]
    scales = {m: s for m in markets if (s := lane_quote_scale(m)) is not None}
    prices = price_panel(con, panels, start=start, scales=scales)
    prices.to_parquet(directory / PRICES_FILE, index=False)
    dates = prices["date"].dt.tz_localize(None).drop_duplicates().sort_values()
    observations = signal_observations(panels)
    priced = set(prices["ticker"].unique())
    observations = observations[observations["ticker"].isin(priced)]
    signal = daily_wide(observations, "profit", dates=dates)
    export.write_pair(signal, directory, SIGNAL_FILE)
    export.write_pair(daily_wide(observations, "profit_raw", dates=dates), directory, SIGNAL_RAW_FILE)
    borrow = pd.DataFrame(columns=["snapshot_date", "ticker", "fee_rate"])
    capped = 0
    if borrow_root is not None:
        borrow = borrow_rates(borrow_root, panels[panels["ticker"].isin(priced)])
        borrow.to_parquet(directory / BORROW_FILE, index=False)
        if rate_cap is not None:
            cap_rates(borrow, rate_cap).to_parquet(directory / capped_rates_file(rate_cap), index=False)
            capped = int((borrow["fee_rate"].astype(float) > rate_cap).sum())
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
        borrow_capped=capped,
    )
