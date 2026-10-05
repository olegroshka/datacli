"""Run EVAL-009 on the AFM long register: (a) the hedge-fund crossings as events, (b) the brokers' synthetic books.

    .venv\\Scripts\\python.exe scripts\\afm_eval.py [--part a|b|both] [--out REPORT.md]

Reads <positioning root>/registers/nl_holdings/ (scripts/afm_issuer_map.py), the
capital register (the probe download) and the ``prices`` view; writes nothing to the
data root. The design is pre-registered in EVAL-009; this script computes it.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from positioning import afm_events as ae  # noqa: E402
from positioning import afm_holdings as ah  # noqa: E402
from positioning import afm_synthetic as asy  # noqa: E402
from positioning import evaluation as ev  # noqa: E402
from positioning import evaluation_register as er  # noqa: E402
from positioning.config import positioning_root  # noqa: E402
from registers import holdings  # noqa: E402

SUBDIR = Path("registers") / "nl_holdings"
CAPITAL = Path("registers") / "_probe_2026-10-05" / "afm_capital.csv"
MARKET_LANE = "nl_domestic"


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join("" if (isinstance(v, float) and np.isnan(v)) else str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def load_closes(con, pairs: list[tuple[str, str]]) -> tuple[dict[tuple[str, str], pd.Series], dict[tuple[str, str], pd.Series]]:
    """Adjusted closes and volumes per (lane, ticker)."""
    out: dict[tuple[str, str], pd.Series] = {}
    volumes: dict[tuple[str, str], pd.Series] = {}
    by_lane: dict[str, list[str]] = {}
    for lane, ticker in pairs:
        by_lane.setdefault(lane, []).append(ticker)
    for lane, tickers in by_lane.items():
        con.register("_afm_tickers", pd.DataFrame({"ticker": sorted(set(tickers))}))
        try:
            frame = con.execute(
                f"SELECT ticker, CAST(date AS DATE) AS date, adjusted_close, volume FROM prices WHERE lane = '{lane}' "
                "AND adjusted_close > 0.0001 AND adjusted_close < 999999 AND ticker IN (SELECT ticker FROM _afm_tickers) ORDER BY ticker, date"
            ).df()
        finally:
            con.unregister("_afm_tickers")
        frame["date"] = pd.to_datetime(frame["date"])
        for ticker, g in frame.groupby("ticker", sort=False):
            idx = pd.DatetimeIndex(g["date"])
            out[(lane, ticker)] = pd.Series(g["adjusted_close"].to_numpy(dtype=float), index=idx)
            volumes[(lane, ticker)] = pd.Series(pd.to_numeric(g["volume"], errors="coerce").to_numpy(dtype=float), index=idx)
    return out, volumes


def market_closes(con, lane: str) -> dict[str, pd.Series]:
    frame = con.execute(
        f"SELECT ticker, CAST(date AS DATE) AS date, adjusted_close FROM prices WHERE lane = '{lane}' AND adjusted_close > 0.0001 AND adjusted_close < 999999 ORDER BY ticker, date"
    ).df()
    frame["date"] = pd.to_datetime(frame["date"])
    return {t: pd.Series(g["adjusted_close"].to_numpy(dtype=float), index=pd.DatetimeIndex(g["date"])) for t, g in frame.groupby("ticker", sort=False)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--part", default="both", choices=("a", "b", "both"))
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    t0 = time.time()
    root = positioning_root()
    directory = root / SUBDIR
    notes = pd.read_parquet(directory / "notifications.parquet")
    issuer_map = pd.read_csv(directory / "issuer_map.csv", dtype=str, keep_default_na=False)
    holders = pd.read_csv(directory / "holders.csv", dtype=str, keep_default_na=False)
    priced = issuer_map[issuer_map["priced"].str.lower() == "true"]
    pairs = [(r.lane, r.ticker) for r in priced.itertuples()]

    import explore_eodhd

    con = explore_eodhd.connect()
    series, volume_series = load_closes(con, pairs)
    closes = {r.issuer: series[(r.lane, r.ticker)] for r in priced.itertuples() if (r.lane, r.ticker) in series}
    volumes = {r.issuer: volume_series[(r.lane, r.ticker)] for r in priced.itertuples() if (r.lane, r.ticker) in volume_series}
    market = ae.market_returns(market_closes(con, MARKET_LANE))
    last_close = max(s.index.max() for s in closes.values())
    print(f"{len(notes):,} notifications, {len(closes)} priced issuers, market {MARKET_LANE} {market.index.min().date()} to {market.index.max().date()}, "
          f"last close {last_close.date()} ({time.time() - t0:.0f}s)")
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 200)
    sections: list[tuple[str, str]] = []

    if args.part in ("a", "both"):
        events = ae.crossings(notes, holders)
        cars = ae.event_cars(events, closes, market)
        in_sample = cars[pd.to_datetime(cars["published_from"]) <= pd.Timestamp(ae.LEDGER_START)]
        led = ae.ledger(cars)
        cov = pd.DataFrame(
            {
                "events": [len(in_sample)],
                "with_series": [int(in_sample["entry_date"].notna().sum())],
                "with_car_21": [int(in_sample["car_21"].notna().sum())],
                "up": [int((in_sample["direction"] == "up").sum())],
                "down": [int((in_sample["direction"] == "down").sum())],
                "potential": [int(in_sample["has_potential"].astype(bool).sum())],
                "ledger_events": [len(led)],
            }
        )
        print("\n(a) events:")
        print(cov.to_string(index=False))
        sections.append(("(a) Events: coverage", _markdown(cov)))
        years = in_sample.assign(year=pd.to_datetime(in_sample["published_from"]).dt.year).groupby(["year", "direction"]).size().unstack(fill_value=0).reset_index()
        sections.append(("(a) Events per year", _markdown(years)))
        main_results = ae.run_events(in_sample)
        table = ae.results_table(main_results)
        table["p"] = table["p"].map(lambda v: f"{v:.2e}")
        print(f"\n(a) EVAL-009a, the two pre-registered tests at {ae.HORIZON} days, Bonferroni p < 0.025:")
        print(table.to_string(index=False))
        sections.append((f"(a) EVAL-009a, pre-registered: 2 tests at {ae.HORIZON} days, Bonferroni p < 0.025; {sum(r.significant for r in main_results)} clear it, "
                         f"{sum(r.significant and r.sign_agrees for r in main_results)} with the sign", _markdown(table)))
        extras = []
        extras += ae.run_events(in_sample, subset="hedge_fund", horizons=(5, 63))
        extras += ae.run_events(in_sample[in_sample["has_potential"].astype(bool)], subset="hedge_fund, potential", horizons=(ae.HORIZON,))
        extras += ae.run_events(in_sample[~in_sample["has_potential"].astype(bool)], subset="hedge_fund, shares only", horizons=(ae.HORIZON,))
        other_events = ae.crossings(notes, holders, kinds=ae.NON_BANK_KINDS)
        other_cars = ae.event_cars(other_events, closes, market)
        other_in = other_cars[pd.to_datetime(other_cars["published_from"]) <= pd.Timestamp(ae.LEDGER_START)]
        extras += ae.run_events(other_in, subset="other and person", horizons=(ae.HORIZON,))
        first, second = ae.halves(in_sample)
        extras += ae.run_events(first, subset="hedge_fund, first half", horizons=(ae.HORIZON,))
        extras += ae.run_events(second, subset="hedge_fund, second half", horizons=(ae.HORIZON,))
        ext = ae.results_table(extras)
        ext["p"] = ext["p"].map(lambda v: f"{v:.2e}")
        print("\n(a) reported without a bar:")
        print(ext.to_string(index=False))
        sections.append(("(a) Reported without a bar (windows, real against potential, the non-bank holders, the halves)", _markdown(ext)))
        pre = []
        for direction in ("up", "down"):
            sub = in_sample[in_sample["direction"] == direction]
            mean, t, n, g = ae.cluster_t(sub["pre_car"], sub["cluster"])
            pre.append(dict(direction=direction, events=n, weeks=g, mean_pre_car_pct=round(100 * mean, 2) if np.isfinite(mean) else np.nan, t=round(t, 2) if np.isfinite(t) else np.nan))
        pre_t = pd.DataFrame(pre)
        print("\n(a) pre-event drift (21 days to the entry close):")
        print(pre_t.to_string(index=False))
        sections.append(("(a) Pre-event drift, 21 days to the entry close (diagnostic)", _markdown(pre_t)))
        if len(led):
            sections.append(("(a) Ledger (not read before 26 weeks)", _markdown(ae.results_table(ae.run_events(led)))))

    if args.part in ("b", "both"):
        brokers = asy.broker_holders(holders)
        all_dates = pd.DatetimeIndex(sorted({d for s in closes.values() for d in s.index}))
        weeks = er.week_ends(all_dates[all_dates >= pd.Timestamp(asy.SAMPLE_START)])
        issuers = sorted(closes)
        panel = asy.synthetic_panel(notes, brokers, weeks, issuers)
        diag = asy.diagnostic(panel)
        summary = pd.DataFrame(
            {
                "brokers (holders)": [len(brokers)],
                "weeks": [len(diag)],
                "issuers per week": [int(diag["issuers"].median())],
                "with a book (median)": [int(diag["with_book"].median())],
                "with a book (min)": [int(diag["with_book"].min())],
                "with a book (max)": [int(diag["with_book"].max())],
                "broker lines (median)": [int(diag["broker_lines"].median())],
                "median book pct": [round(float(diag["median_pct"].median()), 2)],
                "p90 book pct": [round(float(diag["p90_pct"].median()), 2)],
            }
        )
        print(f"\n(b) brokers: {sorted(brokers)}")
        print("(b) diagnostic (per week, medians over weeks):")
        print(summary.T.to_string(header=False))
        sections.append(("(b) Diagnostic: the synthetic books per week", _markdown(summary) + "\n\nBrokers: " + ", ".join(sorted(brokers))))
        by_year = diag.assign(year=pd.to_datetime(diag["date"]).dt.year).groupby("year").agg(weeks=("date", "size"), issuers=("issuers", "median"), with_book=("with_book", "median"),
                                                                                              broker_lines=("broker_lines", "median"), median_pct=("median_pct", "median")).round(2).reset_index()
        print(by_year.to_string(index=False))
        sections.append(("(b) Diagnostic by year", _markdown(by_year)))
        capital = holdings.parse_capital(root / CAPITAL)
        shares = asy.shares_visible(capital)
        obs = asy.observations(panel, closes, shares=shares, volumes=volumes)
        obs = obs[obs["date"] <= pd.Timestamp(ae.LEDGER_START)]
        names = obs.groupby("date").size()
        print(f"\n(b) observations {len(obs):,} over {obs['date'].nunique()} weeks; names per week median {int(names.median())}, min {int(names.min())}; "
              f"has_log_mcap {obs['log_mcap'].notna().mean():.3f}; has_log_adv {obs['log_adv'].notna().mean():.3f}; has_factors {obs['momentum_12_1'].notna().mean():.3f}; "
              f"rows with a positive book {float((obs['synthetic_pct'] > 0).mean()):.3f}")
        results = asy.run_family(obs)
        table = ev.results_table(results)
        table["p"] = table["p"].map(lambda v: f"{v:.2e}")
        sig = [r for r in results if r.significant]
        head = f"(b) EVAL-009b, pre-registered: {len(results)} tests, Bonferroni p < {0.05 / len(results):.1e}; {len(sig)} clear it, {sum(r.sign_agrees for r in sig)} with the sign"
        print("\n" + head)
        print(table.to_string(index=False))
        sections.append((head, _markdown(table)))
        first, second = er.halves(obs)
        for label, part in (("first half", first), ("second half", second)):
            res = asy.run_family(part)
            t = ev.results_table(res)
            t["p"] = t["p"].map(lambda v: f"{v:.2e}")
            sections.append((f"(b) Gate: {label} ({part['date'].min().date()} to {part['date'].max().date()})", _markdown(t)))
        # post-hoc, labelled: liquidity in the cleaning, and the ordering within the hedged names only
        ext = asy.run_family(obs, cleanings=asy.EXTENDED_CLEANINGS)
        ext_t = ev.results_table(ext)
        ext_t["p"] = ext_t["p"].map(lambda v: f"{v:.2e}")
        print("\n(b) post-hoc extension: liquidity in the cleaning (6 tests, no bar):")
        print(ext_t.to_string(index=False))
        sections.append(("(b) Post-hoc extension: factors, size and liquidity in the cleaning (no bar)", _markdown(ext_t)))
        hedged = obs[obs["synthetic_pct"] > 0].copy()
        within = asy.run_family(hedged, features=("synthetic_pct",), cleanings=(*asy.CLEANINGS, *asy.EXTENDED_CLEANINGS), min_names=15)
        within_t = ev.results_table(within)
        within_t["p"] = within_t["p"].map(lambda v: f"{v:.2e}")
        print("\n(b) post-hoc: the level within the hedged names only (15 names a date, no bar):")
        print(within_t.to_string(index=False))
        sections.append(("(b) Post-hoc: the ordering within the names with a positive book (15 names a date, no bar)", _markdown(within_t)))
        groups = []
        obs["_grp"] = np.where(obs["synthetic_pct"] <= 0, "no book", "book")
        for g, sub in obs.groupby("_grp"):
            daily = sub.groupby("date")["ret_adj_21"].mean()
            m, t = ev.newey_west_t(daily, lag=er.nw_lag(21))
            groups.append(dict(group=g, rows=len(sub), issuers_per_week=round(float(sub.groupby("date").size().median()), 1), mean_adj_ret_21_bp=round(1e4 * m, 1), t=round(t, 2)))
        groups_t = pd.DataFrame(groups)
        print("\n(b) post-hoc: 21-day adjusted return by book against no book:")
        print(groups_t.to_string(index=False))
        sections.append(("(b) Post-hoc: the names with a book against the names without (21-day median-adjusted return, per-date means, Newey-West t)", _markdown(groups_t)))

    print(f"\ndone ({time.time() - t0:.0f}s)")
    if args.out:
        head = f"# EVAL-009 run {pd.Timestamp.now():%Y-%m-%d %H:%M}\n\nreturns to {last_close.date()}; {len(closes)} priced issuers; market {MARKET_LANE}\n"
        args.out.write_text(head + "\n".join(f"\n## {title}\n\n{body}\n" for title, body in sections), encoding="utf-8")
        print("report:", args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
