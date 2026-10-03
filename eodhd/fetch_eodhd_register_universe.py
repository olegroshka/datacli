"""Build a register lane's universe (``--lane nl_domestic`` and the others in ``register_lanes.LANE_SPECS``).

Usage:
    uv run python eodhd/fetch_eodhd_register_universe.py --lane se_domestic [--plan]

Outputs:
    data/raw/eodhd/<lane>/tickers_<MARKET>.parquet
    data/raw/eodhd/<lane>/register_unmatched.csv
"""

from __future__ import annotations

import logging
import sys

from register_lanes import LANE_SPECS, take_lane, universe_main

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S")


def main() -> None:
    name = take_lane(sys.argv)
    universe_main(LANE_SPECS[name], logging.getLogger(f"eodhd_{name}_universe"), __doc__ or "")


if __name__ == "__main__":
    main()
