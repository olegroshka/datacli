"""Write the btest inputs for the short-accumulation cost-realism backtest (DD-002 WP15).

    .venv\\Scripts\\python.exe scripts\\export_btest_inputs.py [--start 2018-01-01] [--min-dollar-adv 5e6] [--second]

Reads the datacli views; writes under <positioning root>/exports/btest/.
``--second`` writes the second iteration's files only (size-residual short
ranks, the long-side cohort signals and prices, the borrow rates) next to the
first iteration's, which must exist.
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

from positioning import export  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--start", default=export.DEFAULT_START)
    parser.add_argument("--min-dollar-adv", type=float, default=export.DEFAULT_MIN_DOLLAR_ADV)
    parser.add_argument("--second", action="store_true", help="write the second iteration's files")
    parser.add_argument("--memory-limit", default="8GB", help="DuckDB memory budget for the joins (spills beyond it)")
    parser.add_argument("--threads", type=int, default=8, help="DuckDB threads")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    # The ASOF joins over the full price table would otherwise take DuckDB's default
    # budget (80 percent of RAM, 25 GiB here); spilling to disk is cheaper than a kill.
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(f"SET threads = {args.threads}")
    if args.second:
        second = export.export_second(con, positioning_root(), start=args.start, min_dollar_adv=args.min_dollar_adv)
        print(
            f"wrote {second.directory}: short size-residual ranks for {second.short_size_symbols:,} symbols; "
            f"long side {second.long_symbols:,} symbols, {second.long_price_rows:,} price rows, "
            f"{second.long_signal_dates:,} signal dates x {second.long_flow_symbols:,} symbols with flow; "
            f"borrow rates for {second.borrow_symbols:,} symbols (snapshot {second.borrow_snapshot}) ({time.time() - t0:.0f}s)"
        )
        return 0
    report = export.export(explore_eodhd.connect(), positioning_root(), start=args.start, min_dollar_adv=args.min_dollar_adv)
    print(
        f"wrote {report.directory}: {report.symbols:,} symbols, {report.price_rows:,} price rows "
        f"{report.first_date} to {report.last_date}; signal {report.signal_dates:,} dates x "
        f"{report.signal_symbols:,} symbols ({time.time() - t0:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
