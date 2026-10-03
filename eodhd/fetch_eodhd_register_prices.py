"""Fetch a register lane's daily prices (``--lane nl_domestic`` and the others in ``register_lanes.LANE_SPECS``).

Usage:
    uv run python eodhd/fetch_eodhd_register_prices.py --lane se_domestic --register-only --include-delisted
    uv run python eodhd/fetch_eodhd_register_prices.py --lane se_domestic --include-delisted

Outputs:
    data/raw/eodhd/<lane>/prices_daily.parquet
    data/raw/eodhd/<lane>/prices_fetch_state.csv
"""

from __future__ import annotations

import sys

from register_lanes import LANE_SPECS, TargetLoader, prices_main, take_lane


def main() -> None:
    import fetch_eodhd_us_etf_prices as base

    name = take_lane(sys.argv)
    spec = LANE_SPECS[name]
    prices_main(base, spec, TargetLoader(spec.tickers_path, spec.exchange), f"eodhd_{name}_prices")


if __name__ == "__main__":
    main()
