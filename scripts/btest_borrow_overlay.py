"""Overlay the Interactive Brokers borrow rates on a btest run (WP15, second iteration).

    .venv\\Scripts\\python.exe scripts\\btest_borrow_overlay.py <btest output dir> [--flat 0.01] [--rates PATH]

Reads the run's returns.parquet, weights.parquet and summary.json and the
borrow_rates.parquet written by scripts/export_btest_inputs.py --second;
prints the run's own metrics, the rate profile of its short book and the
metrics with the per-name rates in place of the flat one. Writes nothing.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from positioning import btest_results as br  # noqa: E402
from positioning import export  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("run", type=Path, help="btest output directory of the run")
    parser.add_argument("--flat", type=float, default=0.01, help="the flat annual borrow rate the run charged")
    parser.add_argument("--rates", type=Path, default=None, help="borrow_rates.parquet (default: the export directory)")
    args = parser.parse_args()
    rates_path = args.rates or (positioning_root() / export.SUBDIR / export.BORROW_FILE)
    returns, weights, summary = br.load_run(args.run)
    rates = br.load_rates(rates_path)
    own = br.summarise(returns)
    adjusted = br.summarise(br.overlay(returns, weights, rates, flat_rate=args.flat))
    profile = br.short_rate_profile(weights, rates)
    short_names = (weights < 0).any()
    covered = float(short_names[short_names].index.isin(rates.index).mean()) if short_names.any() else float("nan")
    print(f"run {args.run.name}: {summary.get('metrics', {}).get('cagr', float('nan')):.4f} CAGR per summary.json")
    print(f"short book names ever held: {int(short_names.sum())}, with a borrow rate: {covered:.1%}")
    print(f"weight-averaged short rate: mean {profile.mean():.2%}, median {profile.median():.2%}, max {profile.max():.2%} a year")
    for label, s in (("flat rate (the run)", own), (f"per-name rates ({rates_path.name})", adjusted)):
        print(f"{label}: total {s.total_return:.1%}, CAGR {s.cagr:.2%}, Sharpe {s.sharpe:.2f}, max drawdown {s.max_drawdown:.1%} over {s.years:.1f} years")
    return 0


if __name__ == "__main__":
    sys.exit(main())
