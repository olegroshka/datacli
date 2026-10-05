"""EVAL-007: nowcast US short interest from daily short volume (DD-004 stage 2, the final check).

    .venv\\Scripts\\python.exe scripts\\short_interest_nowcast_eval.py [--fit-to 2021-12-31] [--validate-to 2023-12-31]
        [--models linear,boost] [--sample N] [--out report.md]

Reads finra_short_interest_float (the prints), finra_short_volume (the daily flow, common
stocks with a priced code) and prices (consolidated volume, adjusted close, the 63-day
median volume); builds the settlement periods (positioning.nowcast_short_interest), the
target (the print's change in days of median volume) and the state and flow blocks; fits
state-only against state-plus-flow, linear and boosted; prints the coverage, the scores
and the flow increments as markdown tables and writes them to --out.
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
from positioning import nowcast_short_interest as ns  # noqa: E402


def load(con) -> tuple[pd.DataFrame, pd.DataFrame]:
    lanes = ", ".join(f"'{lane}'" for lane in ns.LANES)
    daily = con.execute(f"""
        WITH px AS (
          SELECT ticker, CAST(date AS DATE) AS date, adjusted_close, volume,
                 median(volume) OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)
                                      ROWS BETWEEN {ns.ADV_WINDOW - 1} PRECEDING AND CURRENT ROW) AS adv,
                 count(volume) OVER (PARTITION BY ticker ORDER BY CAST(date AS DATE)
                                     ROWS BETWEEN {ns.ADV_WINDOW - 1} PRECEDING AND CURRENT ROW) AS adv_n
          FROM prices
          WHERE lane IN ({lanes}) AND adjusted_close > 0 AND close > 0.0001 AND close < 999999
            AND CAST(date AS DATE) >= DATE '2018-01-01'
        )
        SELECT CAST(v.date AS DATE) AS date, v.eodhd_code AS symbol, v.short_volume, v.short_exempt_volume, v.total_volume,
               p.volume AS cvolume, p.adjusted_close, CASE WHEN p.adv_n >= {ns.ADV_MIN_PERIODS} THEN p.adv END AS adv
        FROM finra_short_volume v
        JOIN px p ON p.ticker = v.eodhd_code AND p.date = CAST(v.date AS DATE)
        WHERE v.security_kind = 'common' AND v.eodhd_code IS NOT NULL
        ORDER BY symbol, date
        """).df()
    short_interest = con.execute("""
        SELECT f.eodhd_code AS symbol, CAST(f.settlement_date AS DATE) AS settlement_date, f.short_position, f.days_to_cover
        FROM finra_short_interest_float f
        JOIN finra_short_interest s ON s.eodhd_code = f.eodhd_code AND s.settlement_date = f.settlement_date
        WHERE s.security_kind = 'common' AND f.eodhd_code IS NOT NULL
        """).df()
    daily["date"] = pd.to_datetime(daily["date"])
    short_interest["settlement_date"] = pd.to_datetime(short_interest["settlement_date"])
    return daily, short_interest


def coverage(frame: pd.DataFrame, masks: dict[str, pd.Series]) -> pd.DataFrame:
    rows = []
    for years, mask in masks.items():
        part = frame[mask]
        rows.append({
            "years": years, "rows": len(part), "symbols": part["symbol"].nunique(), "prints": part["settlement_date"].nunique(),
            "y mean": float(part["y"].mean()), "y sd": float(part["y"].std()), "y p1": float(part["y"].quantile(0.01)), "y p99": float(part["y"].quantile(0.99)),
            "sv_adv mean": float(part["sv_adv"].mean()), "days mean": float(part["days"].mean()),
        })
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fit-to", default=ns.FIT_TO)
    parser.add_argument("--validate-to", default=ns.VALIDATE_TO)
    parser.add_argument("--models", default="linear,boost")
    parser.add_argument("--sample", type=int, default=0)
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--memory-limit", default="8GB")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    con.execute(f"SET memory_limit = '{args.memory_limit}'")
    daily, short_interest = load(con)
    print(f"daily rows {len(daily):,} ({daily['symbol'].nunique():,} symbols, {daily['date'].min().date()} to {daily['date'].max().date()}); "
          f"prints {len(short_interest):,} rows ({short_interest['settlement_date'].nunique()} settlement dates) ({time.time() - t0:.0f}s)", flush=True)
    windows = ns.period_windows(short_interest["settlement_date"].unique(), daily["date"].unique())
    flows = ns.flow_features(daily, windows)
    del daily
    frame = ns.assemble(short_interest, flows, windows)
    print(f"periods {len(windows)}; assembled {len(frame):,} symbol-periods, {frame['symbol'].nunique():,} symbols ({time.time() - t0:.0f}s)", flush=True)
    if args.sample and args.sample < len(frame):
        frame = frame.sample(args.sample, random_state=0).reset_index(drop=True)
    masks = nr.split(frame, fit_to=args.fit_to, validate_to=args.validate_to)
    print(f"boosted model backend: {nr.boost_backend()}", flush=True)
    cov = coverage(frame, masks)
    print(nr.markdown(cov))
    table, increments = ns.run(frame, models=tuple(args.models.split(",")), fit_to=args.fit_to, validate_to=args.validate_to)
    print(nr.markdown(table))
    print(nr.markdown(increments))
    print(f"({time.time() - t0:.0f}s)")
    if args.out:
        header = (
            f"# EVAL-007 run ({time.strftime('%Y-%m-%d %H:%M')}; fit to {args.fit_to}, validate to {args.validate_to}, test after; "
            f"boosted model {nr.boost_backend()})\n\n"
        )
        text = header + "## Coverage\n\n" + nr.markdown(cov) + "\n\n## Scores\n\n" + nr.markdown(table) + "\n\n## Flow increments\n\n" + nr.markdown(increments) + "\n"
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
