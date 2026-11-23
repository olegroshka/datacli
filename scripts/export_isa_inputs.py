"""Write the price file for btest's strategies/isa books: daily total-return bars in pounds.

    .venv\\Scripts\\python.exe scripts\\export_isa_inputs.py [--out <dir>] [--start 2011-06-01]

Reads local snapshots only (the EODHD lanes under the configured data root and the FRED observations
beside them); it makes no network call. Writes isa_prices_daily.parquet to <btest>/data/raw/isa/exports/.

Every series becomes pounds at the adjusted close, so dividends are in the return:
  - London lines quoted in pence are divided by 100;
  - dollar lines are converted at GBPUSD: FRED's DEXUSUK while it lasts, then the ratio of the USD and
    pence lines of one gold ETC (IGLN and SGLN are the same security, so their ratio at the same London
    close is the exchange rate);
  - Equinor is the NYSE ADR in dollars, a stand-in for the Oslo line's return in pounds, because the local
    data has no Norwegian krone rate. The ADR is the price series only: an ISA buys the Oslo line. Its
    history starts in 2017, which sets the start of any book that holds it.
Bars are put on the London calendar, the last known price carried over a foreign holiday.
"""

from __future__ import annotations

import argparse
import os
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO / "eodhd") not in sys.path:
    sys.path.insert(0, str(_REPO / "eodhd"))
import price_fixes  # noqa: E402  (the Alpaca-checked fixes of the US daily lanes)

# A daily move this large in a total-return series is a data fault until shown otherwise.
MAX_DAILY_MOVE = 0.5
# The gold-line rate must agree with FRED where both exist, or the splice is not to be trusted.
MAX_SPLICE_ERROR = 0.005
SPLICE_CHECK_DAYS = 60


@dataclass(frozen=True)
class Line:
    ticker: str
    lane: str
    exchange: str
    currency: str  # the quote: GBX, GBP or USD


LINES = (
    Line("SWDA", "uk_eu_etf", "LSE", "GBX"),  # world equities
    Line("CNX1", "uk_eu_etf", "LSE", "GBX"),  # Nasdaq-100
    Line("SGLN", "uk_eu_etf", "LSE", "GBX"),  # physical gold
    Line("IGLT", "uk_eu_etf", "LSE", "GBP"),  # UK gilts
    Line("XWES", "uk_eu_etf", "LSE", "GBP"),  # world energy
    Line("ERNS", "uk_eu_etf", "LSE", "GBP"),  # ultrashort bonds: the cash proxy, for excess returns only
    Line("IGLS", "uk_eu_etf", "LSE", "GBP"),  # short gilts: the cash proxy before ERNS listed
    Line("MU", "us_common", "US", "USD"),
    Line("EQNR", "us_extended", "US", "USD"),
)


def data_root() -> Path:
    env = os.environ.get("EODHD_DATA_ROOT")
    if env:
        return Path(env)
    with open(_REPO / "datacli.toml", "rb") as handle:
        return Path(tomllib.load(handle)["eodhd"]["data_root"])


def implied_gbpusd(usd_line: pd.Series, pence_line: pd.Series) -> pd.Series:
    """Dollars per pound from one security quoted in dollars and in pence."""
    return (usd_line / (pence_line / 100.0)).dropna().rename("gbpusd")


def splice_gbpusd(fred: pd.Series, implied: pd.Series) -> pd.Series:
    """FRED while it lasts, then the implied rate; refuses a splice the overlap does not support."""
    fred = fred.dropna().sort_index()
    implied = implied.dropna().sort_index()
    overlap = pd.concat([fred.rename("fred"), implied.rename("implied")], axis=1, sort=True).dropna().tail(SPLICE_CHECK_DAYS)
    if overlap.empty:
        raise ValueError("no overlap between FRED and the implied rate")
    error = float((overlap["implied"] / overlap["fred"] - 1.0).abs().median())
    if error > MAX_SPLICE_ERROR:
        raise ValueError(f"implied GBPUSD is {error:.2%} from FRED over the overlap; refusing to splice")
    return pd.concat([fred, implied[implied.index > fred.index[-1]]]).rename("gbpusd")


def to_pounds_total_return(bars: pd.DataFrame, currency: str, gbpusd: pd.Series | None = None) -> pd.DataFrame:
    """Scale a line's bars to pounds at its adjusted close. ``bars`` is indexed by date."""
    if currency == "GBX":
        unit = pd.Series(0.01, index=bars.index)
    elif currency == "GBP":
        unit = pd.Series(1.0, index=bars.index)
    elif currency == "USD":
        if gbpusd is None:
            raise ValueError("a dollar line needs GBPUSD")
        rate = gbpusd.reindex(bars.index.union(gbpusd.index)).ffill().reindex(bars.index)
        if rate.isna().any():
            raise ValueError(f"no GBPUSD for {int(rate.isna().sum())} bars, first {rate[rate.isna()].index[0]}")
        unit = 1.0 / rate
    else:
        raise ValueError(f"unknown currency {currency!r}")
    factor = (bars["adjusted_close"] / bars["close"]) * unit
    out = pd.DataFrame({field: bars[field] * factor for field in ("open", "high", "low", "close")})
    out["volume"] = bars["volume"]
    return out


def on_calendar(frame: pd.DataFrame, calendar: pd.DatetimeIndex) -> pd.DataFrame:
    """The last known bar on each calendar day, starting at the line's first bar; no bar is invented before it."""
    frame = frame.sort_index()
    joined = frame.reindex(frame.index.union(calendar))
    prices = joined[["open", "high", "low", "close"]].copy()
    prices["close"] = prices["close"].ffill()
    for field in ("open", "high", "low"):
        prices[field] = prices[field].fillna(prices["close"])
    prices["volume"] = joined["volume"].fillna(0.0)
    return prices.reindex(calendar).dropna(subset=["close"])


def read_line(con, root: Path, line: Line) -> pd.DataFrame:
    path = (root / line.lane / "prices_daily.parquet").as_posix()
    source = price_fixes.overlay_sql(f"read_parquet('{path}')", line.lane, price_fixes.fixes_path(root))
    frame = con.execute(
        f"select date,open,high,low,close,adjusted_close,volume from {source} where ticker=? and exchange=?",
        [line.ticker, line.exchange],
    ).df()
    if frame.empty:
        raise ValueError(f"{line.ticker} has no bars in {line.lane}")
    frame["date"] = pd.to_datetime(frame["date"])
    return frame.set_index("date").sort_index()


def main() -> int:
    import duckdb

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=None, help="default: <btest>/data/raw/isa/exports")
    parser.add_argument("--start", default="2011-06-01")
    args = parser.parse_args()

    root = data_root()
    out_dir = Path(args.out) if args.out else root.parents[0] / "isa" / "exports"
    con = duckdb.connect()

    fred_path = (root.parent / "macro" / "fred_observations.parquet").as_posix()
    fred = con.execute(f"select date,value from read_parquet('{fred_path}') where series_id='DEXUSUK' and value is not null").df()
    fred["date"] = pd.to_datetime(fred["date"])
    fred = fred.set_index("date")["value"].sort_index()
    gold_usd = read_line(con, root, Line("IGLN", "uk_eu_etf", "LSE", "USD"))["close"]
    gold_gbx = read_line(con, root, Line("SGLN", "uk_eu_etf", "LSE", "GBX"))["close"]
    gbpusd = splice_gbpusd(fred, implied_gbpusd(gold_usd, gold_gbx))

    raw = {line.ticker: read_line(con, root, line) for line in LINES}
    calendar = raw["SWDA"].loc[args.start :].index
    frames = []
    for line in LINES:
        bars = on_calendar(to_pounds_total_return(raw[line.ticker], line.currency, gbpusd), calendar)
        worst = float(bars["close"].pct_change(fill_method=None).abs().max())
        if worst > MAX_DAILY_MOVE:
            raise ValueError(f"{line.ticker} moved {worst:.0%} in a day; check the source")
        print(f"{line.ticker:5} {line.currency} {bars.index[0].date()} to {bars.index[-1].date()} {len(bars):>5} bars, largest day {worst:.1%}, last GBP {bars['close'].iloc[-1]:.2f}")
        frames.append(bars.assign(ticker=line.ticker))

    prices = pd.concat(frames).rename_axis("date").reset_index()
    prices["date"] = prices["date"].dt.tz_localize("UTC")
    prices["sector"] = "isa"
    prices["volume"] = prices["volume"].astype("int64")
    prices = prices[["date", "ticker", "close", "open", "high", "low", "volume", "sector"]]
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "isa_prices_daily.parquet"
    prices.to_parquet(target, index=False)
    print(f"GBPUSD: FRED to {fred.index[-1].date()}, then the gold lines; last {gbpusd.iloc[-1]:.4f} on {gbpusd.index[-1].date()}")
    print(f"wrote {target}: {len(prices):,} rows, {prices['ticker'].nunique()} lines")
    return 0


if __name__ == "__main__":
    sys.exit(main())
