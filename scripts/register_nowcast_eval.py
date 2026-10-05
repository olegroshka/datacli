"""EVAL-006: nowcast the register from public ripples, the European registers as the oracle (DD-004 iteration 1).

    .venv\\Scripts\\python.exe scripts\\register_nowcast_eval.py [--markets uk,fr,nl,se,de] [--fit-to 2019-12-31]
        [--validate-to 2022-12-31] [--models linear,boost] [--targets t1,t2,t3,t4] [--sample N] [--out report.md]

Reads <positioning root>/registers/<market>/{issuers,funds}.parquet (scripts/register_panel.py)
and the market's lane bars (prices); assembles the issuer-days (positioning.nowcast_register),
fits the state-only and state-plus-ripples models per target, prints the base rates, the
scores and the ripple increments as markdown tables, and writes them to --out.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[1]
for p in (_REPO, _REPO / "eodhd"):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from positioning import nowcast_register as nr  # noqa: E402
from positioning import register_panel  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def load_market(con, root: Path, market: str) -> pd.DataFrame:
    base = root / register_panel.SUBDIR / market
    issuers = pd.read_parquet(base / "issuers.parquet")
    funds = pd.read_parquet(base / "funds.parquet")
    lane, _ = register_panel.MARKET_LANES[market]
    tickers = sorted(issuers["ticker"].dropna().unique())
    con.register("_nowcast_tickers", pd.DataFrame({"ticker": tickers}))
    try:
        prices = con.execute(f"""
            SELECT CAST(p.date AS DATE) AS date, p.ticker, p.high, p.low, p.close, p.adjusted_close, p.volume
            FROM prices p JOIN _nowcast_tickers t USING (ticker)
            WHERE p.lane = '{lane}' AND p.close > 0 AND p.adjusted_close > 0
            ORDER BY p.ticker, date
            """).df()
    finally:
        con.unregister("_nowcast_tickers")
    prices["date"] = pd.to_datetime(prices["date"])
    return nr.assemble(issuers, funds, prices)


def base_rates(frame: pd.DataFrame, masks: dict[str, pd.Series]) -> pd.DataFrame:
    rows = []
    for years, mask in masks.items():
        part = frame[mask]
        rows.append({
            "years": years, "issuer_days": len(part), "issuers": part["isin"].nunique(),
            "t1 rate": float(part["t1"].mean()), "t2 rate": float(part["t2"].mean()),
            "t3 nonzero": float((part["t3"] != 0).mean()), "t4 up": float((part["t4"] == 1).mean()), "t4 down": float((part["t4"] == 2).mean()),
            "ripples present": float(part["abn_vol"].notna().mean()),
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markets", default=",".join(nr.MARKETS))
    parser.add_argument("--fit-to", default=nr.FIT_TO)
    parser.add_argument("--validate-to", default=nr.VALIDATE_TO)
    parser.add_argument("--models", default="linear,boost")
    parser.add_argument("--targets", default=",".join(nr.TARGETS))
    parser.add_argument("--sample", type=int, default=0, help="fit on a random sample of this many issuer-days (0 = all)")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--ripples-only", action="store_true", help="keep only the issuer-days whose ripple block is present (a labelled post-hoc check)")
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    markets = [m.strip() for m in args.markets.split(",") if m.strip()]
    frames = []
    for market in markets:
        frame = load_market(con, positioning_root(), market)
        print(f"{market}: {len(frame):,} issuer-days, {frame['isin'].nunique():,} issuers, {frame['date'].min().date()} to {frame['date'].max().date()}", flush=True)
        frames.append(frame)
    frame = pd.concat(frames, ignore_index=True)
    if args.ripples_only:
        frame = frame[frame["abn_vol"].notna()].reset_index(drop=True)
        print(f"ripples-only: {len(frame):,} issuer-days kept", flush=True)
    if args.sample and args.sample < len(frame):
        frame = frame.sample(args.sample, random_state=0).reset_index(drop=True)
    masks = nr.split(frame, fit_to=args.fit_to, validate_to=args.validate_to)
    print(f"boosted model backend: {nr.boost_backend()}", flush=True)
    rates = base_rates(frame, masks)
    print(nr.markdown(rates))
    table, increments = nr.run(
        frame, targets_to_run=tuple(args.targets.split(",")), models=tuple(args.models.split(",")), fit_to=args.fit_to, validate_to=args.validate_to
    )
    print(nr.markdown(table))
    print(nr.markdown(increments))
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        header = (
            f"# EVAL-006 run ({time.strftime('%Y-%m-%d %H:%M')}; markets {', '.join(markets)}; fit to {args.fit_to}, "
            f"validate to {args.validate_to}, test after; boosted model {nr.boost_backend()})\n\n"
        )
        text = header + "## Base rates\n\n" + nr.markdown(rates) + "\n\n## Scores\n\n" + nr.markdown(table) + "\n\n## Ripple increments\n\n" + nr.markdown(increments) + "\n"
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
