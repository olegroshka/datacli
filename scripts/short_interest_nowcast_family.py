"""EVAL-007, the conditional step: the daily short-inventory nowcast and EVAL-001's family on it against the published timing.

    .venv\\Scripts\\python.exe scripts\\short_interest_nowcast_family.py [--fit-to 2021-12-31] [--from 2022-01-01] [--out report.md]

Builds the periods, the full-period and partial-period boosted models on the prints up to --fit-to
(positioning.nowcast_short_interest, positioning.nowcast_daily), the daily nowcast ``si_hat`` next
to the published ``si_pub`` for every symbol-day, EVAL-001's two path features on each, and the
twelve-test family per timing at daily decision dates from --from on (forward returns 10 and 21
days from the next close, market-adjusted; cleanings none, factors, factors+level); prints the
tables and the nowcast's gain at ten days.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd", _REPO / "scripts"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import nowcast_daily as nd  # noqa: E402
from positioning import nowcast_register as nr  # noqa: E402
from positioning import nowcast_short_interest as ns  # noqa: E402
from short_interest_nowcast_eval import load  # noqa: E402


def returns_and_factors(con, series: pd.DataFrame, *, horizons=nd.HORIZONS) -> pd.DataFrame:
    lanes = ", ".join(f"'{lane}'" for lane in ns.LANES)
    max_h = max(horizons)
    con.register("_series", series[["symbol", "date"]])
    exits = ", ".join(f"x{h}.adjusted_close / e.adjusted_close - 1 AS ret_{h}" for h in horizons)
    joins = " ".join(f"LEFT JOIN px x{h} ON x{h}.ticker = s.symbol AND x{h}.rn = d.rn + 1 + {h}" for h in horizons)
    try:
        out = con.execute(f"""
            WITH px AS (
              SELECT ticker, CAST(date AS DATE) AS date, adjusted_close,
                     row_number() OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)) AS rn
              FROM prices
              WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
                AND CAST(date AS DATE) >= DATE '2018-01-01'
            )
            SELECT s.symbol, s.date, {exits}, f.reversal_21d, f.momentum_12_1
            FROM _series s
            JOIN px d ON d.ticker = s.symbol AND d.date = CAST(s.date AS DATE)
            LEFT JOIN px e ON e.ticker = s.symbol AND e.rn = d.rn + 1
            {joins}
            LEFT JOIN positioning_factors f ON f.ticker = s.symbol AND f.date = CAST(s.date AS DATE)
            """).df()
    finally:
        con.unregister("_series")
    out["date"] = pd.to_datetime(out["date"])
    for h in horizons:
        out[f"ret_adj_{h}"] = out[f"ret_{h}"] - out.groupby("date")[f"ret_{h}"].transform("median")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fit-to", default=ns.FIT_TO)
    parser.add_argument("--from", dest="from_date", default="2022-01-01")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    daily, short_interest = load(con)
    prints = con.execute("""
        SELECT f.eodhd_code AS symbol, CAST(f.settlement_date AS DATE) AS settlement_date, CAST(f.published_at AS DATE) AS published_at, f.short_position
        FROM finra_short_interest_float f JOIN finra_short_interest s ON s.eodhd_code = f.eodhd_code AND s.settlement_date = f.settlement_date
        WHERE s.security_kind = 'common' AND f.eodhd_code IS NOT NULL
        """).df()
    windows = ns.period_windows(short_interest["settlement_date"].unique(), daily["date"].unique())
    full = ns.assemble(short_interest, ns.flow_features(daily, windows), windows)
    partial = nd.partial_rows(nd.partial_flow_features(daily, windows), full)
    del daily
    print(f"periods {len(full):,}; partial rows {len(partial):,} ({time.time() - t0:.0f}s); backend {nr.boost_backend()}", flush=True)
    full_model, partial_model = nd.fit_models(full, partial, fit_to=args.fit_to)
    print(f"models fitted on prints to {args.fit_to} ({time.time() - t0:.0f}s)", flush=True)
    series = nd.daily_nowcast(partial, full, prints, full_model=full_model, partial_model=partial_model)
    del partial
    series = nd.path_features(series)
    series = series[series["date"] >= pd.Timestamp(args.from_date)].reset_index(drop=True)
    print(f"symbol-days from {args.from_date}: {len(series):,} ({series['symbol'].nunique():,} symbols, {series['date'].nunique()} dates) ({time.time() - t0:.0f}s)", flush=True)
    frame = series.merge(returns_and_factors(con, series), on=["symbol", "date"], how="left")
    frame["level"] = frame["si_pub"] / frame["adv"].where(frame["adv"] > 0)
    print(f"returns joined ({time.time() - t0:.0f}s)", flush=True)
    table = nd.family(frame)
    gain = nd.gains(table)
    agreement = (
        frame[["date", "si_hat", "si_pub"]].assign(diff=lambda f: (f["si_hat"] / f["si_pub"].where(f["si_pub"] > 0) - 1.0).abs())
        .groupby("date")["diff"].median().describe()[["25%", "50%", "75%"]].round(4).to_dict()
    )
    print("median |si_hat/si_pub - 1| per date, quantiles over dates:", agreement)
    print(nr.markdown(table))
    print(nr.markdown(gain))
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        header = (
            f"# EVAL-007 conditional step ({time.strftime('%Y-%m-%d %H:%M')}; models fit on prints to {args.fit_to}, "
            f"decision dates daily from {args.from_date}; boosted model {nr.boost_backend()})\n\n"
            f"Symbol-days {len(series):,}, symbols {series['symbol'].nunique():,}, dates {series['date'].nunique()}; "
            f"median absolute nowcast-to-published gap per date, quantiles over dates: {agreement}\n\n"
        )
        args.out.write_text(header + "## Family (twelve tests per timing, Bonferroni p < 4.2e-3)\n\n" + nr.markdown(table) + "\n\n## Gain of the nowcast (mean IC, nowcast minus published)\n\n" + nr.markdown(gain) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
