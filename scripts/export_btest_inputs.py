"""Write the btest inputs for the short-accumulation cost-realism backtest (DD-002 WP15).

    .venv\\Scripts\\python.exe scripts\\export_btest_inputs.py [--start 2018-01-01] [--min-dollar-adv 5e6]

Reads the datacli views; writes under <positioning root>/exports/btest/.
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
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    report = export.export(explore_eodhd.connect(), positioning_root(), start=args.start, min_dollar_adv=args.min_dollar_adv)
    print(
        f"wrote {report.directory}: {report.symbols:,} symbols, {report.price_rows:,} price rows "
        f"{report.first_date} to {report.last_date}; signal {report.signal_dates:,} dates x "
        f"{report.signal_symbols:,} symbols ({time.time() - t0:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
