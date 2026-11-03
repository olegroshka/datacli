"""Walk-forward test of next-day AMD forecasts, and the inputs for btest's sized run of them.

    .venv\\Scripts\\python.exe scripts\\chip_nextday_forecast.py [--live] [--out <dir>]

Pools 16 chip stocks and forecasts the close-to-close log return of the next session from: price and
volume behaviour, the chip sector, market and macro proxy funds, FRED series and news sentiment. Models,
feature sets and the primary model are fixed below, before any result is seen; nothing chooses among them.
Timeline: features at close t, target close t to close t+1; models refit every REFIT sessions on data
ending before the block, with the scaler inside the pipeline. FRED is lagged one business day. News uses
UTC dates up to t-1, because the rollup for date t holds articles published after the close.
The chip list is today's survivors, which flatters any result; read a weak result as weak.

Reads local snapshots only, unless --live appends one delayed EODHD quote per ticker (24 calls) to forecast
the next session. Writes to <btest>/data/raw/isa/exports/: nextday_predictions.parquet, nextday_results.csv
and amd_prices_daily.parquet, which strategies/isa/next_day_timing.py sizes with the broker's costs.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tomllib
import urllib.request
import warnings
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
CHIPS = ("AMD", "NVDA", "MU", "AVGO", "QCOM", "INTC", "TXN", "AMAT", "LRCX", "KLAC", "MRVL", "ON", "ADI", "NXPI", "MCHP", "TSM")
FUNDS = ("SPY", "QQQ", "SMH", "TLT", "HYG", "UUP", "USO", "GLD")
FRED = ("VIXCLS", "VXVCLS", "T10Y2Y", "BAMLH0A0HYM2", "DGS10", "DCOILBRENTEU")
OOS_START = "2014-01-01"
NEWS_START = "2019-02-01"
NEWS_OOS_START = "2021-07-01"
REFIT = 126
COST = 0.0010  # a side, for a 14,000 pound position: commission, FX and half the spread
PRIMARY = "S3_proxies|pool|ridge"

OWN = ["r1", "r5", "r21", "r63", "vol21", "vol5_21", "gap", "range_pos", "range", "vol_z", "vwap5", "vwap21", "vol_shock", "dist252"]
SECTOR = ["SMH_r1", "SMH_r5", "rel1", "rel5"]
PROXY = ["SPY_r1", "SPY_r5", "QQQ_r1", "QQQ_r5", "TLT_r1", "TLT_r5", "HYG_r1", "HYG_r5", "UUP_r5", "USO_r1", "USO_r5", "GLD_r5"]
MACRO = ["vix", "vix_chg5", "vix_term", "curve", "hy", "hy_chg21", "y10_chg5", "brent_r21"]
NEWS = ["pol1", "pol5", "news_z", "sector_pol"]
SETS = {
    "S1_own": OWN,
    "S2_sector": OWN + SECTOR,
    "S3_proxies": OWN + SECTOR + PROXY,
    "S4_fred": OWN + SECTOR + PROXY + MACRO,
    "S5_news": OWN + SECTOR + PROXY + NEWS,
}


def data_root() -> Path:
    env = os.environ.get("EODHD_DATA_ROOT")
    if env:
        return Path(env)
    with open(_REPO / "datacli.toml", "rb") as handle:
        return Path(tomllib.load(handle)["eodhd"]["data_root"])


def stock_features(bars: pd.DataFrame) -> pd.DataFrame:
    """Price and volume features for one stock; each row uses bars up to and including its own date."""
    bars = bars.sort_index()
    k = bars["a"] / bars["close"]
    opened, high, low, close = bars["open"] * k, bars["high"] * k, bars["low"] * k, bars["a"]
    typical = (high + low + close) / 3
    volume = bars["volume"].replace(0, np.nan)
    log_close = np.log(close)
    r1 = log_close.diff()
    x = pd.DataFrame(index=bars.index)
    x["r1"], x["r5"], x["r21"], x["r63"] = r1, log_close.diff(5), log_close.diff(21), log_close.diff(63)
    x["vol21"] = r1.rolling(21).std()
    x["vol5_21"] = r1.rolling(5).std() / x["vol21"]
    x["gap"] = np.log(opened / close.shift(1))
    x["range_pos"] = (close - low) / (high - low).replace(0, np.nan)
    x["range"] = (high - low) / close
    log_volume = np.log(volume)
    x["vol_z"] = (log_volume - log_volume.rolling(63).mean().shift(1)) / log_volume.rolling(63).std().shift(1)
    for window in (5, 21):
        x[f"vwap{window}"] = close / ((typical * volume).rolling(window).sum() / volume.rolling(window).sum()) - 1
    x["vol_shock"] = x["r1"] * x["vol_z"]
    x["dist252"] = close / close.rolling(252).max() - 1
    return x.replace([np.inf, -np.inf], np.nan)


def macro_features(funds: pd.DataFrame, fred: pd.DataFrame) -> pd.DataFrame:
    """Fund returns at the same close; FRED is shifted one session because it publishes the next day."""
    log_funds = np.log(funds)
    x = pd.DataFrame(index=funds.index)
    for name in ("SPY", "QQQ", "SMH", "TLT", "HYG", "USO"):
        x[f"{name}_r1"] = log_funds[name].diff()
    for name in FUNDS:
        x[f"{name}_r5"] = log_funds[name].diff(5)
    lagged = fred.reindex(funds.index.union(fred.index)).sort_index().ffill().reindex(funds.index).shift(1)
    x["vix"] = lagged["VIXCLS"]
    x["vix_chg5"] = lagged["VIXCLS"].diff(5)
    x["vix_term"] = lagged["VIXCLS"] / lagged["VXVCLS"]
    x["curve"] = lagged["T10Y2Y"]
    x["hy"] = lagged["BAMLH0A0HYM2"]
    x["hy_chg21"] = lagged["BAMLH0A0HYM2"].diff(21)
    x["y10_chg5"] = lagged["DGS10"].diff(5)
    x["brent_r21"] = np.log(lagged["DCOILBRENTEU"]).diff(21)
    return x


def news_features(index: pd.DatetimeIndex, polarity: pd.Series, articles: pd.Series) -> pd.DataFrame:
    """Sentiment known by the close of each date: only UTC dates strictly before it."""
    span = index.union(polarity.index).union(articles.index).sort_values()
    pol = polarity.reindex(span)
    count = np.log1p(articles.reindex(span).fillna(0.0))
    x = pd.DataFrame(index=span)
    x["pol1"] = pol.shift(1)
    x["pol5"] = pol.shift(1).rolling(5, min_periods=2).mean()
    x["news_z"] = (count.shift(1) - count.shift(2).rolling(63).mean()) / count.shift(2).rolling(63).std()
    return x.reindex(index).replace([np.inf, -np.inf], np.nan)  # a stretch with no change in article counts


def model(kind: str):
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    if kind == "ridge":
        return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=5000.0))
    return HistGradientBoostingRegressor(max_depth=3, learning_rate=0.03, max_iter=150, min_samples_leaf=300, l2_regularization=10.0, random_state=0)


def walk_forward(panel: pd.DataFrame, cols: list[str], kind: str, scope: str, start: str = OOS_START) -> pd.Series:
    data = panel if scope == "pool" else panel[panel["ticker"] == "AMD"]
    amd = panel[panel["ticker"] == "AMD"]
    dates = amd.loc[start:].dropna(subset=["y"]).index
    with_news = "pol1" in cols
    if with_news:
        dates = dates[dates >= pd.Timestamp(NEWS_OOS_START)]
    out = pd.Series(index=dates, dtype=float)
    for first in range(0, len(dates), REFIT):
        block = dates[first : first + REFIT]
        # A row's target ends at the next session, so rows before the block's previous session are all known.
        train = data[data.index < block[0] - pd.Timedelta(days=1)].dropna(subset=["y"])
        if with_news:
            train = train[train.index >= pd.Timestamp(NEWS_START)]
        use = [c for c in cols if train[c].notna().any()]
        fitted = model(kind).fit(train[use], train["y"].clip(-0.15, 0.15))
        out.loc[block] = fitted.predict(amd.loc[block][use])
    return out


def score(pred: pd.Series, y: pd.Series) -> dict:
    y = y.reindex(pred.index)
    half = len(pred) // 2
    position = (pred > 0).astype(float)
    changes = position.diff().abs().fillna(position.iloc[0])
    strategy = np.exp(position * y) - 1 - changes * COST
    hold = np.exp(y) - 1

    def annual(r):
        return (1 + r).prod() ** (252 / len(r)) - 1

    def sharpe(r):
        return r.mean() / r.std() * 252**0.5

    def drawdown(r):
        curve = (1 + r).cumprod()
        return (curve / curve.cummax() - 1).min()

    return {
        "n": len(pred),
        "R2_vs0": 1 - ((y - pred) ** 2).sum() / (y**2).sum(),
        "IC": pred.corr(y, method="spearman"),
        "IC_h1": pred.iloc[:half].corr(y.iloc[:half], method="spearman"),
        "IC_h2": pred.iloc[half:].corr(y.iloc[half:], method="spearman"),
        "hit": (np.sign(pred) == np.sign(y)).mean(),
        "upDays": (y > 0).mean(),
        "pctLong": position.mean(),
        "stratCAGR": annual(strategy),
        "stratSharpe": sharpe(strategy),
        "stratMaxDD": drawdown(strategy),
        "holdCAGR": annual(hold),
        "holdSharpe": sharpe(hold),
        "holdMaxDD": drawdown(hold),
        "trades_py": changes.sum() / (len(strategy) / 252),
    }


def block_interval(a: np.ndarray, b: np.ndarray, draws: int = 1000, length: int = 21) -> np.ndarray:
    rng = np.random.default_rng(3)
    n = len(a)
    values = []
    for _ in range(draws):
        starts = rng.integers(0, n, n // length + 1)
        pick = np.concatenate([(np.arange(length) + s) % n for s in starts])[:n]
        values.append(pd.Series(a[pick]).corr(pd.Series(b[pick]), method="spearman"))
    return np.percentile(values, [5, 95])


def deflated_sharpe(returns: pd.Series, trial_sharpes: list[float], trials: int) -> float:
    nd = NormalDist()
    euler = 0.5772156649
    benchmark = float(np.std(trial_sharpes, ddof=1)) * ((1 - euler) * nd.inv_cdf(1 - 1 / trials) + euler * nd.inv_cdf(1 - 1 / (trials * np.e)))
    sr = returns.mean() / returns.std()
    return nd.cdf((sr - benchmark) * (len(returns) - 1) ** 0.5 / (1 - returns.skew() * sr + (returns.kurt() + 2) / 4 * sr * sr) ** 0.5)


def live_bars(names, key: str) -> pd.DataFrame:
    rows = []
    for name in names:
        url = f"https://eodhd.com/api/real-time/{name}.US?api_token={key}&fmt=json"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}), timeout=20) as resp:
                quote = json.loads(resp.read())
        except Exception as ex:
            raise RuntimeError(f"live quote failed for {name}: {str(ex).replace(key, '<key>')[:120]}") from None
        when = pd.to_datetime(quote["timestamp"], unit="s").normalize()
        rows.append({"date": when, "ticker": name, "open": quote["open"], "high": quote["high"], "low": quote["low"], "close": quote["close"], "a": quote["close"], "volume": quote["volume"]})
    return pd.DataFrame(rows)


def load(root: Path, live: bool):
    import duckdb

    con = duckdb.connect()

    def bars(lane, names):
        path = (root / lane / "prices_daily.parquet").as_posix()
        listed = ",".join(repr(n) for n in names)
        frame = con.execute(f"select date,ticker,open,high,low,close,adjusted_close a,volume from read_parquet('{path}') where exchange='US' and ticker in ({listed})").df()
        frame["date"] = pd.to_datetime(frame["date"])
        return frame

    stocks, funds = bars("us_common", CHIPS), bars("us_etf", FUNDS)
    if live:
        key = os.environ.get("EODHD_API_KEY", "")
        if not key:
            raise RuntimeError("--live needs EODHD_API_KEY")
        extra = live_bars(CHIPS + FUNDS, key)
        print("live bars dated", extra["date"].max().date(), "AMD", extra[extra.ticker == "AMD"][["open", "high", "low", "close", "volume"]].to_dict("records"))
        stocks = pd.concat([stocks, extra[extra.ticker.isin(CHIPS)]])
        funds = pd.concat([funds, extra[extra.ticker.isin(FUNDS)]])
    fred_path = (root.parent / "macro" / "fred_observations.parquet").as_posix()
    fred = con.execute(f"select series_id,date,value from read_parquet('{fred_path}') where series_id in ({','.join(repr(n) for n in FRED)})").df()
    fred["date"] = pd.to_datetime(fred["date"])
    news_path = (root / "news" / "news_symbol_daily.parquet").as_posix()
    news = con.execute(f"select cast(date as varchar) d,ticker,sum(n_articles) n,sum(polarity_mean*n_articles)/nullif(sum(n_articles),0) pol from read_parquet('{news_path}') where exchange='US' and ticker in ({','.join(repr(n) for n in CHIPS)}) group by 1,2").df()
    news["date"] = pd.to_datetime(news["d"])
    earnings_path = (root / "us_common" / "earnings_history.parquet").as_posix()
    earnings = con.execute(f"select ticker,report_date,before_after_market bam,surprise_percent sp from read_parquet('{earnings_path}') where exchange='US' and ticker in ({','.join(repr(n) for n in CHIPS)}) and eps_actual is not null and before_after_market is not null").df()
    earnings["report_date"] = pd.to_datetime(earnings["report_date"])
    return stocks, funds, fred, news, earnings


def build_panel(stocks, funds, fred, news) -> pd.DataFrame:
    wide = funds.pivot_table(index="date", columns="ticker", values="a").sort_index().ffill()
    macro = macro_features(wide, fred.pivot_table(index="date", columns="series_id", values="value").sort_index())
    polarity = news.pivot_table(index="date", columns="ticker", values="pol")
    articles = news.pivot_table(index="date", columns="ticker", values="n").fillna(0.0)
    frames = []
    for ticker, group in stocks.groupby("ticker"):
        bars = group.set_index("date").sort_index()
        x = stock_features(bars).join(macro)
        x["rel1"], x["rel5"] = x["r1"] - x["SMH_r1"], x["r5"] - x["SMH_r5"]
        x = x.join(news_features(x.index, polarity[ticker], articles[ticker]))
        x["ticker"] = ticker
        x["y"] = x["r1"].shift(-1)
        frames.append(x)
    panel = pd.concat(frames).sort_index()
    return panel.join(panel.groupby(level=0)["pol1"].mean().rename("sector_pol"))


def earnings_table(panel: pd.DataFrame, earnings: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for ticker, group in panel.groupby("ticker"):
        days = group.index
        previous = np.nan
        for _, event in earnings[earnings.ticker == ticker].sort_values("report_date").iterrows():
            i = days.searchsorted(event["report_date"], side="right" if event["bam"] == "AfterMarket" else "left")
            if i >= len(days) or i < 64:
                continue
            before = group.iloc[i - 1]
            rows.append({"ticker": ticker, "day": days[i], "react": group["r1"].iloc[i], "pre1": before["r1"], "pre5": before["r5"], "pre21": before["r21"], "pre63": before["r63"], "vol_z": before["vol_z"], "vwap5": before["vwap5"], "smh5": before["SMH_r5"], "qqq5": before["QQQ_r5"], "dist252": before["dist252"], "vol21": before["vol21"], "prev_react": previous})
            previous = group["r1"].iloc[i]
    table = pd.DataFrame(rows).dropna(subset=["react", "pre1"]).sort_values("day")
    return table[table["day"] >= "2010-01-01"]


EARNINGS_COLS = ["pre1", "pre5", "pre21", "pre63", "vol_z", "vwap5", "smh5", "qqq5", "dist252", "vol21", "prev_react"]


def earnings_model():
    from sklearn.impute import SimpleImputer
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler

    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(), Ridge(alpha=300.0))


def main() -> int:
    warnings.filterwarnings("ignore")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--live", action="store_true", help="append one delayed EODHD quote per ticker (24 calls)")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    pd.set_option("display.width", 230, "display.max_columns", 30, "display.max_rows", 80)

    root = data_root()
    out_dir = Path(args.out) if args.out else root.parent / "isa" / "exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    stocks, funds, fred, news, earnings = load(root, args.live)
    panel = build_panel(stocks, funds, fred, news)
    amd = panel[panel["ticker"] == "AMD"]
    target = amd["y"]

    results, predictions = {}, {}
    for set_name, cols in SETS.items():
        for scope in ("pool", "amd"):
            for kind in ("ridge", "gbm"):
                label = f"{set_name}|{scope}|{kind}"
                predictions[label] = walk_forward(panel, cols, kind, scope)
                results[label] = score(predictions[label], target)
    table = pd.DataFrame(results).T
    print(f"=== walk-forward, AMD next-day return, {len(table)} trials, cost {COST:.2%} a side, signal and fill at the same close ===")
    print(table.round(4).to_string())
    print(f"\nprimary {PRIMARY}: IC {table.loc[PRIMARY, 'IC']:.4f}, 90% block interval {block_interval(predictions[PRIMARY].values, target.reindex(predictions[PRIMARY].index).values).round(4)}")
    best = table["IC"].idxmax()
    print(f"best IC {best}: {table.loc[best, 'IC']:.4f}, interval {block_interval(predictions[best].values, target.reindex(predictions[best].index).values).round(4)}")

    sharpes, nets = [], {}
    for label, pred in predictions.items():
        y = target.reindex(pred.index)
        position = (pred > 0).astype(float)
        nets[label] = np.exp(position * y) - 1 - position.diff().abs().fillna(position.iloc[0]) * COST
        sharpes.append(nets[label].mean() / nets[label].std())
    best_net = table["stratSharpe"].idxmax()
    trials = len(sharpes) + 1
    excess = nets[best_net] - (np.exp(target.reindex(nets[best_net].index)) - 1)
    print(f"best strategy {best_net}: Sharpe {table.loc[best_net, 'stratSharpe']:.2f} vs hold {table.loc[best_net, 'holdSharpe']:.2f}; deflated Sharpe {deflated_sharpe(nets[best_net], sharpes, trials):.3f} (N={trials}); minus hold {excess.mean() * 252:+.3f} a year, t {excess.mean() / excess.std() * len(excess) ** 0.5:.2f}")

    print("\n=== how the chips move together ===")
    for label, sub in (("2014 on", amd.loc["2014":]), ("last 3 years", amd.loc["2023-11":])):
        print(label, "same-day corr with SMH", round(sub["r1"].corr(sub["SMH_r1"]), 3), "| next day: SMH", round(sub["SMH_r1"].corr(sub["y"]), 3), "own", round(sub["r1"].corr(sub["y"]), 3), "volume shock", round(sub["vol_shock"].corr(sub["y"]), 3), "vwap5", round(sub["vwap5"].corr(sub["y"]), 3))

    print("\n=== earnings reactions ===")
    events = earnings_table(panel, earnings)
    events = events[events["day"] < amd.index[-1]]
    for label, sub in (("all chips", events), ("AMD", events[events.ticker == "AMD"]), ("AMD since 2020", events[(events.ticker == "AMD") & (events.day >= "2020")])):
        print(f"{label}: n={len(sub)} up {(sub['react'] > 0).mean():.0%} median {sub['react'].median():+.2%} median abs {sub['react'].abs().median():.2%} p10 {sub['react'].quantile(.1):+.2%} p90 {sub['react'].quantile(.9):+.2%} | corr with day before {sub['react'].corr(sub['pre1']):.3f}")
    oos = events[events["day"] >= "2016-01-01"].copy()
    oos["pred"] = np.nan
    for _, block in oos.groupby(pd.PeriodIndex(oos["day"], freq="Q")):
        fitted = earnings_model().fit(events[events["day"] < block["day"].min()][EARNINGS_COLS], events[events["day"] < block["day"].min()]["react"].clip(-0.3, 0.3))
        oos.loc[block.index, "pred"] = fitted.predict(block[EARNINGS_COLS])
    only = oos[oos.ticker == "AMD"]
    print(f"earnings model, walk-forward: panel n={len(oos)} IC {oos['pred'].corr(oos['react'], method='spearman'):.3f} hit {(np.sign(oos['pred']) == np.sign(oos['react'])).mean():.3f}; AMD n={len(only)} IC {only['pred'].corr(only['react'], method='spearman'):.3f} hit {(np.sign(only['pred']) == np.sign(only['react'])).mean():.3f}")

    last = amd.index[-1]
    print(f"\n=== next session, features to {last.date()} ===")
    train = panel[panel.index < last].dropna(subset=["y"])
    for label, cols, kind, data in (("primary", SETS["S3_proxies"], "ridge", train), ("S3 gbm", SETS["S3_proxies"], "gbm", train), ("S5 news ridge", SETS["S5_news"], "ridge", train[train.index >= NEWS_START])):
        print(label, f"{float(model(kind).fit(data[cols], data['y'].clip(-0.15, 0.15)).predict(amd.loc[[last]][cols])[0]):+.3%}")

    pd.DataFrame(predictions).rename_axis("date").to_parquet(out_dir / "nextday_predictions.parquet")
    table.to_csv(out_dir / "nextday_results.csv")
    raw = stocks[stocks["ticker"] == "AMD"].set_index("date").sort_index().loc["2013-01-01":]
    k = raw["a"] / raw["close"]
    prices = pd.DataFrame({"ticker": "AMD", "close": raw["a"], "open": raw["open"] * k, "high": raw["high"] * k, "low": raw["low"] * k, "volume": raw["volume"].astype("int64"), "sector": "chips"}, index=raw.index).rename_axis("date").reset_index()
    prices["date"] = prices["date"].dt.tz_localize("UTC")
    prices.to_parquet(out_dir / "amd_prices_daily.parquet", index=False)
    print(f"\nwrote nextday_predictions.parquet, nextday_results.csv and amd_prices_daily.parquet to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
