"""The long leg of the register profit ordering alone, against the market and the factors (DD-003 WP22).

    .venv\\Scripts\\python.exe scripts\\register_long_leg.py [--markets de,fr,nl,ie] [--freq W|M] [--edge 0.2] [--cap 0.04] [--out REPORT.md]

Reads the WP21 export under <positioning root>/exports/btest/ (register_prices_daily,
register_profit) and the markets' issuer panels; prints the leg's gross return against the
equal-weight universe, the equal-weight book of every name with a visible holder, and the
factor portfolios (market, size, momentum, reversal) with Newey-West statistics; writes the
leg's daily weights to register_long_leg_weights.parquet for btest's cost-realism run.
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

import pandas as pd  # noqa: E402

from positioning import register_export as rx  # noqa: E402
from positioning import register_long_leg as ll  # noqa: E402
from positioning.config import positioning_root  # noqa: E402

WEIGHTS_FILE = "register_long_leg_weights.parquet"


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markets", default=",".join(rx.EUR_MARKETS))
    parser.add_argument("--freq", default="W", choices=["W", "M"])
    parser.add_argument("--edge", type=float, default=ll.EDGE)
    parser.add_argument("--cap", type=float, default=ll.CAP)
    parser.add_argument("--start", default="2015-01-01")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    t0 = time.time()
    root = positioning_root()
    directory = root / rx.SUBDIR
    prices = pd.read_parquet(directory / rx.PRICES_FILE)
    signal = pd.read_parquet(directory / rx.SIGNAL_FILE)
    returns = ll.daily_returns(prices)
    start = pd.Timestamp(args.start)
    returns = returns.loc[start:]
    signal = signal.reindex(index=returns.index, columns=returns.columns)
    universe = returns.notna()

    weights = ll.long_leg_weights(signal, edge=args.edge, cap=args.cap, freq=args.freq)
    short_side = ll.long_leg_weights(-signal, edge=args.edge, cap=args.cap, freq=args.freq)  # the top of the ranks, for symmetry
    leg = ll.portfolio_return(weights, returns)
    top = ll.portfolio_return(short_side, returns)
    crowded = ll.portfolio_return(ll.long_leg_weights(signal.notna().astype(float) * -1.0, edge=0.5, cap=1.0, freq=args.freq), returns)
    ew = ll.portfolio_return(ll.equal_weight(universe), returns)
    median_name = returns.median(axis=1).fillna(0.0)

    panels = rx.load_panels(root, tuple(m.strip() for m in args.markets.split(",")))
    adv = ll.dollar_volume_adv(prices)
    momentum = panels.pivot_table(index="date", columns="ticker", values="momentum_12_1").reindex(index=returns.index, columns=returns.columns)
    reversal = panels.pivot_table(index="date", columns="ticker", values="reversal_21d").reindex(index=returns.index, columns=returns.columns)
    factors = ll.factor_returns(returns, adv=adv.reindex(index=returns.index, columns=returns.columns), momentum=momentum, reversal=reversal, universe=universe, freq="M")

    names = int((weights.loc[weights.index[::5]] > 0).sum(axis=1).median())
    print(f"leg: {names} names on a median rebalance (edge {args.edge}, cap {args.cap}, {args.freq}); {len(leg)} days {returns.index.min().date()} to {returns.index.max().date()} ({time.time() - t0:.0f}s)")
    summary = pd.DataFrame(
        [(label, *ll.annualised(s)) for label, s in (
            ("long leg (losing shorts, bottom ranks)", leg), ("top ranks (the shorts in the money)", top),
            ("every name with a visible holder, equal weight", crowded), ("equal-weight universe", ew), ("median name", median_name),
            ("size (small minus big)", factors["size"]), ("momentum (12-1)", factors["mom"]), ("reversal (21-day winners minus losers)", factors["rev"]))],
        columns=["series", "return a year", "Sharpe"],
    )
    summary["return a year"] = (summary["return a year"] * 100).round(2)
    summary["Sharpe"] = summary["Sharpe"].round(2)
    print("\n== gross returns (no costs)")
    print(summary.to_string(index=False))

    x_mkt = factors[["mkt"]]
    x_all = factors[["mkt", "size", "mom", "rev"]]
    rows = [
        ("long leg vs market", ll.regress(leg, x_mkt)),
        ("long leg vs market, size, momentum, reversal", ll.regress(leg, x_all)),
        ("long leg minus the crowded book vs the factors", ll.regress(leg - crowded, x_all)),
        ("crowded book (every shorted name) vs the factors", ll.regress(crowded, x_all)),
        ("top ranks vs the factors", ll.regress(top, x_all)),
        ("long leg minus top ranks vs the factors", ll.regress(leg - top, x_all)),
    ]
    table = ll.regression_table(rows)
    print("\n== regressions, daily, Newey-West lag 5 (alpha in percent a year)")
    print(table.to_string(index=False))
    halves = []
    mid = returns.index[len(returns.index) // 2]
    for label, sl in (("first half", slice(None, mid)), ("second half", slice(mid, None))):
        r = ll.regress(leg.loc[sl], x_all.loc[sl])
        c = ll.regress((leg - crowded).loc[sl], x_all.loc[sl])
        halves.append({"half": label, "from": str(leg.loc[sl].index.min().date()), "to": str(leg.loc[sl].index.max().date()),
                       "leg alpha": round(r.alpha_annual * 100, 2), "t": round(r.alpha_t, 2),
                       "leg minus crowded alpha": round(c.alpha_annual * 100, 2), "t ": round(c.alpha_t, 2)})
    halves_table = pd.DataFrame(halves)
    print("\n== halves")
    print(halves_table.to_string(index=False))

    weights.to_parquet(directory / WEIGHTS_FILE)
    print(f"\nwrote {directory / WEIGHTS_FILE} for btest")
    if args.out:
        lines = [f"# Long leg alone (positioning.register_long_leg, {args.freq}, edge {args.edge}, cap {args.cap})", "",
                 f"Run {time.strftime('%Y-%m-%d %H:%M')}; {names} names on a median rebalance; {len(leg)} days {returns.index.min().date()} to {returns.index.max().date()}.", "",
                 "## Gross returns", "", _markdown(summary), "", "## Regressions (daily, Newey-West lag 5, alpha in percent a year)", "", _markdown(table), "",
                 "## Halves", "", _markdown(halves_table), ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
