"""EVAL-008: the HARP-style decomposition of a positioning panel and the family on its layers (DD-005 iteration 1).

    .venv\\Scripts\\python.exe scripts\\panel_decomposition_eval.py --panel us|eu_issuer|eu_fund [--out report.md]

us        : the FINRA prints (the short position in days of 63-day median volume, log1p), blocks by
            liquidity quintile at the window's end, windows of 72 prints refit every 6; decision at the
            print's publication plus one trading day; returns 10 and 21 days, market-adjusted; the
            factors of positioning_factors at the publication date; 200 names a date, Newey-West lag 2.
eu_issuer : the registers' issuer level (log1p), weekly (Fridays), blocks by market, windows of 156
            weeks refit every 13; decision the Friday, returns from the next close, market-adjusted
            within the market; the panel's own factors; 50 names a date, Newey-West lag ceil(h/5).
eu_fund   : the per-fund panel (a holder's position in an issuer, log1p of percent, weekly, zero when
            absent), blocks by holder; the surprise and the common component aggregated to the issuer
            (position-weighted and by count) and tested on eu_issuer's dates and returns.

Each run prints HARP's out-of-sample R² per year (a diagnostic), the family (18 tests, Bonferroni),
the split halves, and the own-history z-score's IC on the same dates (the baseline the surprise
must beat by 0.005); writes them to --out.
"""

from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import nowcast_register as nr  # noqa: E402  (markdown)
from positioning import panel_decomposition as pdc  # noqa: E402
from positioning import register_panel  # noqa: E402
from positioning.config import positioning_root  # noqa: E402

REGISTERS = ("uk", "de", "fr", "se", "nl")
US_WINDOW, US_STEP = 72, 6
EU_WINDOW, EU_STEP = 156, 13
FEATURES = {"us": ("eps", "c", "e"), "eu_issuer": ("eps", "c", "e"), "eu_fund": ("eps_fund", "c_fund", "eps_fund_n")}


# --------------------------------------------------------------------------- #
# US prints
# --------------------------------------------------------------------------- #
def us_inputs(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    prints = con.execute("""
        WITH adv AS (
          SELECT ticker, CAST(date AS DATE) AS date,
                 median(volume) OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE) ROWS BETWEEN 62 PRECEDING AND CURRENT ROW) AS adv
          FROM prices WHERE lane IN ('us_common','us_extended') AND CAST(date AS DATE) >= DATE '2017-06-01' AND close > 0.0001)
        SELECT f.eodhd_code AS symbol, CAST(f.settlement_date AS DATE) AS date, CAST(f.published_at AS DATE) AS published_at,
               f.short_position / a.adv AS level, a.adv
        FROM finra_short_interest_float f
        JOIN finra_short_interest s ON s.eodhd_code = f.eodhd_code AND s.settlement_date = f.settlement_date AND s.security_kind = 'common'
        ASOF JOIN adv a ON a.ticker = f.eodhd_code AND a.date <= CAST(f.settlement_date AS DATE)
        WHERE a.adv > 1000
        """).df()
    prints["date"] = pd.to_datetime(prints["date"])
    prints["published_at"] = pd.to_datetime(prints["published_at"])
    prints["x"] = np.log1p(prints["level"].clip(lower=0))
    return prints, prints.pivot_table(index="date", columns="symbol", values="x")


def us_returns(con, obs: pd.DataFrame) -> pd.DataFrame:
    con.register("_obs", obs[["symbol", "published_at"]].drop_duplicates())
    try:
        out = con.execute("""
            WITH px AS (
              SELECT ticker, CAST(date AS DATE) AS date, adjusted_close,
                     row_number() OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)) AS rn
              FROM prices WHERE lane IN ('us_common','us_extended') AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
                AND CAST(date AS DATE) >= DATE '2017-06-01')
            SELECT o.symbol, o.published_at, e.date AS entry_date,
                   x10.adjusted_close / e.adjusted_close - 1 AS ret_10, x21.adjusted_close / e.adjusted_close - 1 AS ret_21,
                   f.reversal_21d, f.momentum_12_1
            FROM _obs o
            ASOF JOIN px e ON e.ticker = o.symbol AND e.date > CAST(o.published_at AS DATE)
            LEFT JOIN px x10 ON x10.ticker = o.symbol AND x10.rn = e.rn + 10
            LEFT JOIN px x21 ON x21.ticker = o.symbol AND x21.rn = e.rn + 21
            LEFT JOIN positioning_factors f ON f.ticker = o.symbol AND f.date = CAST(o.published_at AS DATE)
            """).df()
    finally:
        con.unregister("_obs")
    out["published_at"] = pd.to_datetime(out["published_at"])
    for h in (10, 21):
        out[f"ret_adj_{h}"] = out[f"ret_{h}"] - out.groupby("published_at")[f"ret_{h}"].transform("median")
    return out


def run_us(con) -> dict[str, pd.DataFrame]:
    prints, panel = us_inputs(con)
    adv = prints.pivot_table(index="date", columns="symbol", values="adv")

    def blocks(last_date: pd.Timestamp) -> pd.Series:
        row = adv.loc[:last_date].iloc[-1].dropna()
        return pd.qcut(row.rank(method="first"), 5, labels=False).astype(int).map(lambda q: f"adv_q{q}")

    dec = pdc.rolling_decomposition(panel, window=US_WINDOW, step=US_STEP, blocks=blocks, k=pdc.K_B, min_block=pdc.MIN_BLOCK)
    z = pdc.own_history_zscore(panel).stack().rename("z_own").reset_index().rename(columns={"level_0": "date", "symbol": "name"})
    z.columns = ["date", "name", "z_own"]
    obs = dec.merge(z, on=["date", "name"], how="left").rename(columns={"name": "symbol"})
    obs = obs.merge(prints[["symbol", "date", "published_at"]], on=["symbol", "date"], how="left")
    obs["level"] = obs["x"]
    rets = us_returns(con, obs)
    frame = obs.merge(rets, on=["symbol", "published_at"], how="left")
    frame["date_obs"] = frame["date"]
    frame["date"] = frame["published_at"]  # the decision date groups the cross-section
    return {"decomposed": dec, "frame": frame}


# --------------------------------------------------------------------------- #
# the European registers
# --------------------------------------------------------------------------- #
def eu_issuer_inputs() -> tuple[pd.DataFrame, pd.DataFrame]:
    root = positioning_root() / register_panel.SUBDIR
    frames = []
    for m in REGISTERS:
        f = pd.read_parquet(root / m / "issuers.parquet", columns=["market", "isin", "date", "level", "adjusted_close", "reversal_21d", "momentum_12_1"])
        f["date"] = pd.to_datetime(f["date"])
        frames.append(f)
    daily = pd.concat(frames, ignore_index=True)
    daily["name"] = daily["market"] + ":" + daily["isin"]
    weekly = daily[daily["date"].dt.weekday == 4].copy()
    weekly["x"] = np.log1p(weekly["level"].clip(lower=0))
    return daily, weekly


def eu_returns(daily: pd.DataFrame, obs: pd.DataFrame) -> pd.DataFrame:
    """Forward returns from the next trading day's close after the Friday, market-adjusted within the market."""
    px = daily.sort_values(["name", "date"]).reset_index(drop=True)
    px["rn"] = px.groupby("name").cumcount()
    key = px[["name", "date", "rn", "adjusted_close"]]
    out = obs[["name", "date", "market"]].merge(key, on=["name", "date"], how="left")
    by_name = {n: g.set_index("rn")["adjusted_close"] for n, g in key.groupby("name")}
    entry, r10, r21 = [], [], []
    for n, rn in zip(out["name"], out["rn"]):
        s = by_name.get(n)
        if s is None or pd.isna(rn) or rn + 22 >= len(s):
            entry.append(np.nan); r10.append(np.nan); r21.append(np.nan); continue
        e = s.get(rn + 1, np.nan)
        entry.append(e)
        r10.append(s.get(rn + 11, np.nan) / e - 1.0 if e and e > 0 else np.nan)
        r21.append(s.get(rn + 22, np.nan) / e - 1.0 if e and e > 0 else np.nan)
    out["ret_10"], out["ret_21"] = r10, r21
    for h in (10, 21):
        out[f"ret_adj_{h}"] = out[f"ret_{h}"] - out.groupby(["market", "date"])[f"ret_{h}"].transform("median")
    return out[["name", "date", "ret_adj_10", "ret_adj_21"]]


def run_eu_issuer(daily: pd.DataFrame, weekly: pd.DataFrame) -> dict[str, pd.DataFrame]:
    panel = weekly.pivot_table(index="date", columns="name", values="x")
    blocks = pd.DataFrame({"block": [n.split(":")[0] for n in panel.columns]}, index=panel.columns)
    dec = pdc.rolling_decomposition(panel, window=EU_WINDOW, step=EU_STEP, blocks=blocks, fill="zero")
    z = pdc.own_history_zscore(panel).stack().reset_index()
    z.columns = ["date", "name", "z_own"]
    obs = dec.merge(z, on=["date", "name"], how="left")
    obs = obs.merge(weekly[["name", "date", "market", "reversal_21d", "momentum_12_1"]], on=["name", "date"], how="left")
    obs["level"] = obs["x"]
    frame = obs.merge(eu_returns(daily, obs), on=["name", "date"], how="left")
    return {"decomposed": dec, "frame": frame}


def run_eu_fund(daily: pd.DataFrame, weekly: pd.DataFrame, issuer_frame: pd.DataFrame) -> dict[str, pd.DataFrame]:
    root = positioning_root() / register_panel.SUBDIR
    frames = []
    for m in REGISTERS:
        f = pd.read_parquet(root / m / "funds.parquet", columns=["market", "holder", "isin", "position_date", "inventory"])
        f["date"] = pd.to_datetime(f["position_date"])
        f["name"] = f["market"] + ":" + f["holder"] + "|" + f["isin"]
        frames.append(f[["name", "date", "inventory", "market", "holder", "isin"]])
    funds = pd.concat(frames, ignore_index=True)
    wide = funds.pivot_table(index="date", columns="name", values="inventory", aggfunc="last").resample("W-FRI").last().ffill().fillna(0.0)
    panel = np.log1p(wide.clip(lower=0))
    blocks = pd.DataFrame({"block": [n.split("|")[0] for n in panel.columns]}, index=panel.columns)
    dec = pdc.rolling_decomposition(panel, window=EU_WINDOW, step=EU_STEP, blocks=blocks, fill="zero")
    dec["issuer"] = dec["name"].str.split("|").str[1]
    dec["market"] = dec["name"].str.split(":").str[0]
    dec["issuer_name"] = dec["market"] + ":" + dec["issuer"]
    dec["weight"] = np.expm1(dec["x"]).clip(lower=0)  # the position in percent
    held = dec[dec["weight"] > 0]
    agg = held.groupby(["issuer_name", "date"]).apply(
        lambda g: pd.Series({
            "eps_fund": float(np.average(g["eps"], weights=g["weight"])) if g["weight"].sum() > 0 else np.nan,
            "c_fund": float(np.average(g["c"], weights=g["weight"])) if g["weight"].sum() > 0 else np.nan,
            "eps_fund_n": float(g["eps"].mean()), "n_funds": len(g),
        }), include_groups=False
    ).reset_index().rename(columns={"issuer_name": "name"})
    frame = agg.merge(issuer_frame[["name", "date", "market", "reversal_21d", "momentum_12_1", "level", "z_own", "ret_adj_10", "ret_adj_21"]], on=["name", "date"], how="inner")
    return {"decomposed": dec, "frame": frame}


# --------------------------------------------------------------------------- #
# reporting
# --------------------------------------------------------------------------- #
def report(name: str, result: dict[str, pd.DataFrame]) -> str:
    frame = result["frame"]
    features = FEATURES[name]
    if name == "us":
        min_names, lag = 200, (lambda h: 2)
    else:
        min_names, lag = 50, (lambda h: int(math.ceil(h / 5)))
    r2 = pdc.forecast_r2(result["decomposed"]) if "f_g0" in result["decomposed"].columns else pd.DataFrame()
    family = pdc.ic_family(frame, features=features, min_names=min_names, nw_lag=lag)
    baseline = pdc.ic_family(frame, features=("z_own",), min_names=min_names, nw_lag=lag, family_size=len(features) * 6)
    first, second = pdc.halves(frame)
    halves = pd.concat([
        pdc.ic_family(first, features=features, min_names=min_names, nw_lag=lag).assign(half="first", from_date=first["date"].min().date(), to_date=first["date"].max().date()),
        pdc.ic_family(second, features=features, min_names=min_names, nw_lag=lag).assign(half="second", from_date=second["date"].min().date(), to_date=second["date"].max().date()),
    ])
    dates = frame["date"].nunique()
    names = frame.groupby("date")["name" if "name" in frame.columns else "symbol"].nunique().median()
    text = (
        f"## {name}: {len(frame):,} observations, {dates} decision dates, {int(names)} names a date (median), "
        f"{frame['date'].min().date()} to {frame['date'].max().date()}\n\n"
        + ("### HARP diagnostics: out-of-sample R² by year\n\n" + nr.markdown(r2) + "\n\n" if len(r2) else "")
        + "### Family (18 tests, Bonferroni p < 2.8e-3)\n\n" + nr.markdown(family) + "\n\n"
        + "### The own-history z-score on the same dates (the baseline)\n\n" + nr.markdown(baseline) + "\n\n"
        + "### Split halves (no bar)\n\n" + nr.markdown(halves) + "\n"
    )
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--panel", choices=("us", "eu_issuer", "eu_fund"), required=True)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()
    t0 = time.time()
    if args.panel == "us":
        import explore_eodhd

        con = explore_eodhd.connect()
        con.execute(f"SET memory_limit = '{args.memory_limit}'")
        result = run_us(con)
    else:
        daily, weekly = eu_issuer_inputs()
        issuer = run_eu_issuer(daily, weekly)
        result = issuer if args.panel == "eu_issuer" else run_eu_fund(daily, weekly, issuer["frame"])
    print(f"decomposed {len(result['decomposed']):,} rows; frame {len(result['frame']):,} ({time.time() - t0:.0f}s)", flush=True)
    text = report(args.panel, result)
    print(text)
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        args.out.write_text(f"# EVAL-008 run, panel {args.panel} ({time.strftime('%Y-%m-%d %H:%M')})\n\n" + text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
