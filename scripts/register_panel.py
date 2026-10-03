"""Build one market's per-fund register ladders and the daily issuer panel (DD-003 WP18).

    .venv\\Scripts\\python.exe scripts\\register_panel.py --market uk

Reads the datacli views; writes <positioning root>/registers/<market>/{funds,issuers}.parquet.
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

from positioning import register_panel  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", default="uk", choices=sorted(register_panel.MARKET_LANES))
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    report = register_panel.build(explore_eodhd.connect(), positioning_root(), args.market)
    print(
        f"{report.market}: {report.register_rows:,} register rows, {report.pairs:,} holder-issuer pairs "
        f"({report.pairs_priced:,} priced), {report.fund_rows:,} fund ladder rows; issuer panel {report.panel_rows:,} rows "
        f"over {report.issuers:,} issuers, {report.first_date} to {report.last_date}; wrote {report.directory} ({time.time() - t0:.0f}s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
