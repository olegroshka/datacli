"""Run the pre-registered short-ladder evaluation (positioning.evaluation) on the local data.

    .venv\\Scripts\\python.exe scripts\\short_ladder_eval.py [--out REPORT.md]

Reads the datacli views only; writes nothing to the data root. Prints the
pre-registered family first, then the labelled post-hoc extension.
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


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="write the report here (markdown)")
    parser.add_argument("--max-seed-share", type=float, default=0.5)
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    panel = ev.load_panel(con)
    print(f"panel rows {len(panel):,} symbols {panel['symbol'].nunique():,} dates {panel['date'].nunique()} ({time.time() - t0:.0f}s)")
    usable = panel[(panel["seed_share"] < args.max_seed_share)]
    usable = usable[usable["entry_date"].notna()]
    print(f"after seed and entry filters: rows {len(usable):,} symbols {usable['symbol'].nunique():,}")
    frame = ev.add_features(usable)
    by_lane = frame.groupby("lane").agg(
        rows=("symbol", "size"),
        symbols=("symbol", "nunique"),
        has_level=("level", lambda s: round(float(s.notna().mean()), 3)),
        has_dtc=("level_dtc", lambda s: round(float(s.notna().mean()), 3)),
        has_factors=("momentum_12_1", lambda s: round(float(s.notna().mean()), 3)),
    )
    print(by_lane.to_string())
    pd.set_option("display.width", 200)
    pd.set_option("display.max_rows", 200)
    tables: dict[str, tuple[pd.DataFrame, int]] = {}
    for label, kwargs in (
        ("pre-registered family", {}),
        (
            "post-hoc extension: days to cover as the level (every row has it)",
            {"features": ev.EXTENDED_FEATURES, "cleanings": ev.EXTENDED_CLEANINGS},
        ),
    ):
        results = ev.run_family(frame, **kwargs)
        table = ev.results_table(results)
        table["p"] = table["p"].map(lambda v: f"{v:.2e}")
        size = results[0].family_size
        sig = [r for r in results if r.significant]
        print(
            f"\n== {label}: {size} tests, Bonferroni p < {0.05 / size:.1e}; "
            f"{len(sig)} clear it, {sum(r.sign_agrees for r in sig)} with SPEC's sign"
        )
        print(table.to_string(index=False))
        tables[label] = (table, size)
    if args.out:
        lines = [
            "# EVAL-001: short ladder, pre-registered family (positioning.evaluation)",
            "",
            f"Run {time.strftime('%Y-%m-%d %H:%M')}; panel {len(panel):,} ladder rows, "
            f"{len(usable):,} after the seed-lot and entry filters; "
            f"dates {usable['date'].min().date()} to {usable['date'].max().date()}.",
            "",
            "Input coverage by lane:",
            "",
            _markdown(by_lane.reset_index()),
            "",
        ]
        for label, (table, size) in tables.items():
            lines += [f"## {label} ({size} tests, bar p < {0.05 / size:.1e})", "", _markdown(table), ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
