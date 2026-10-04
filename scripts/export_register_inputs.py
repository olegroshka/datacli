"""Write the btest inputs for the sized run of the register profit ordering (DD-003 WP21).

    .venv\\Scripts\\python.exe scripts\\export_register_inputs.py [--markets de,fr,nl,ie] [--start 2013-01-01] [--no-borrow]

Reads <positioning root>/registers/<market>/issuers.parquet (scripts/register_panel.py),
the register lanes' prices and the broker's country files (borrow fetch --country all);
writes register_prices_daily, register_profit(_raw)(_neg) and register_borrow_rates
under <positioning root>/exports/btest/.
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

from borrow.config import borrow_root  # noqa: E402
from positioning import register_export as rx  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--markets", default=",".join(rx.EUR_MARKETS))
    parser.add_argument("--start", default=rx.DEFAULT_START)
    parser.add_argument("--no-borrow", action="store_true", help="skip the borrow rates file")
    parser.add_argument("--memory-limit", default="8GB")
    parser.add_argument("--threads", type=int, default=8)
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    con.execute(f"SET threads = {args.threads}")
    markets = tuple(m.strip() for m in args.markets.split(",") if m.strip())
    report = rx.export_registers(
        con, positioning_root(), markets=markets, start=args.start, borrow_root=None if args.no_borrow else borrow_root()
    )
    print(
        f"wrote {report.directory}: markets {', '.join(report.markets)}; {report.symbols:,} symbols, "
        f"{report.price_rows:,} price rows {report.first_date} to {report.last_date}; signal on "
        f"{report.signal_dates:,} dates x {report.signal_symbols:,} symbols; borrow rates for "
        f"{report.borrow_symbols:,} symbols (snapshot {report.borrow_snapshot}) ({time.time() - t0:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
