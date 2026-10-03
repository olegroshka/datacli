"""Run the pre-registered long-side evaluation (EVAL-002, positioning.evaluation_long).

    .venv\\Scripts\\python.exe scripts\\long_ladder_eval.py [--out REPORT.md]

Reads the datacli views only; writes nothing to the data root. Prints the
primary family (every 13F filer), then the secondary one (the hedge-fund
cohort before its 2025-12-31 break), each with its own Bonferroni bar.
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

from positioning import evaluation as ev  # noqa: E402
from positioning import evaluation_long as evl  # noqa: E402


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="write the report here (markdown)")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    panel = evl.load_panel(con)
    print(f"panel rows {len(panel):,} securities {panel['symbol'].nunique():,} periods {panel['date'].nunique()} ({time.time() - t0:.0f}s)")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    sections: list[tuple[str, pd.DataFrame, int, str]] = []
    for aggregate, label in (("all", "primary: every 13F filer"), ("cohort", "secondary: hedge-fund cohort to 2025-09-30 (underpowered)")):
        frame = evl.usable(panel, aggregate)
        print(f"\n{label}: rows {len(frame):,} securities {frame['symbol'].nunique():,} periods {frame['date'].nunique()}")
        frame = evl.add_features(frame)
        coverage = frame.groupby("lane").agg(
            rows=("symbol", "size"),
            securities=("symbol", "nunique"),
            has_level=("level", lambda s: round(float(s.notna().mean()), 3)),
            has_factors=("momentum_12_1", lambda s: round(float(s.notna().mean()), 3)),
            has_holdings=("long_fund_weight", lambda s: round(float(s.notna().mean()), 3)),
            has_z=("z_inventory", lambda s: round(float(s.notna().mean()), 3)),
        )
        print(coverage.to_string())
        for suffix, kwargs in (("", {}), (" | post-hoc extension: size control", {"cleanings": evl.EXTENDED_CLEANINGS})):
            results = evl.run_family(frame, **kwargs)
            table = ev.results_table(results)
            table["p"] = table["p"].map(lambda v: f"{v:.2e}")
            size = results[0].family_size
            sig = [r for r in results if r.significant]
            print(
                f"\n== {label}{suffix}: {size} tests, Bonferroni p < {0.05 / size:.1e}; "
                f"{len(sig)} clear it, {sum(r.sign_agrees for r in sig)} with SPEC's sign"
            )
            print(table.to_string(index=False))
            sections.append((label + suffix, table, size, _markdown(coverage.reset_index())))
    if args.out:
        lines = [
            "# EVAL-002 run (positioning.evaluation_long)",
            "",
            f"Run {time.strftime('%Y-%m-%d %H:%M')}; panel {len(panel):,} ladder rows over both aggregates, "
            f"{panel['symbol'].nunique():,} securities, periods {panel['date'].min().date()} to {panel['date'].max().date()}.",
            "",
        ]
        for label, table, size, coverage in sections:
            lines += [f"## {label} ({size} tests, bar p < {0.05 / size:.1e})", "", "Coverage by lane:", "", coverage, "", _markdown(table), ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
