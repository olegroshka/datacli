"""Run the pre-registered EVAL-003 family (positioning.evaluation_ownership).

    .venv\\Scripts\\python.exe scripts\\ownership_eval.py [--out REPORT.md]

Reads the datacli views only; writes nothing to the data root. Prints the
primary family (ownership share and continuing-manager flow given size, both
aggregates pooled, 12 tests), the secondary crowding family (8 tests), the
split-half and size-tercile gates, and the out-of-sample ledger from the
2026-09-30 period once it holds enough dates.
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
from positioning import evaluation_ownership as evo  # noqa: E402


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def _table(results: list[ev.TestResult], aggregate: str) -> pd.DataFrame:
    table = ev.results_table(results)
    table.insert(0, "aggregate", aggregate)
    table["p"] = table["p"].map(lambda v: f"{v:.2e}")
    return table


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None, help="write the report here (markdown)")
    args = parser.parse_args()

    import explore_eodhd

    t0 = time.time()
    con = explore_eodhd.connect()
    panel = evo.load_panel(con)
    print(f"panel rows {len(panel):,} securities {panel['symbol'].nunique():,} periods {panel['date'].nunique()} ({time.time() - t0:.0f}s)")
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    sections: list[tuple[str, str]] = []
    frames = {a: evo.prepare(panel, a) for a in evo.AGGREGATES}
    coverage = []
    for aggregate, frame in frames.items():
        coverage.append(
            {
                "aggregate": aggregate,
                "rows": len(frame),
                "securities": frame["symbol"].nunique(),
                "dates": frame["date"].nunique(),
                "has_level": round(float(frame["level"].notna().mean()), 3),
                "has_size": round(float(frame["log_mcap"].notna().mean()), 3),
                "has_flow": round(float(frame["flow"].notna().mean()), 3),
                "has_factors": round(float(frame["momentum_12_1"].notna().mean()), 3),
            }
        )
    coverage_table = pd.DataFrame(coverage)
    print(coverage_table.to_string(index=False))
    sections.append(("Coverage (usable rows per aggregate)", _markdown(coverage_table)))

    for label, spec, size in (("primary: ownership share and flow given size", evo.PRIMARY, evo.PRIMARY_SIZE),
                              ("secondary: crowding given size and ownership", evo.SECONDARY, evo.SECONDARY_SIZE)):
        results = {a: evo.run_spec(frames[a], spec, size=size) for a in evo.AGGREGATES}
        table = pd.concat([_table(results[a], a) for a in evo.AGGREGATES], ignore_index=True)
        flat = [r for rs in results.values() for r in rs]
        sig = [r for r in flat if r.significant]
        head = (f"{label}: {size} tests, Bonferroni p < {0.05 / size:.1e}; "
                f"{len(sig)} clear it, {sum(r.sign_agrees for r in sig)} with the pre-registered sign")
        print(f"\n== {head}")
        print(table.to_string(index=False))
        sections.append((head, _markdown(table)))

    half_rows = []
    for aggregate, frame in frames.items():
        for name, part in zip(("first half", "second half"), evo.halves(frame)):
            table = _table(evo.run_spec(part, evo.PRIMARY, size=evo.PRIMARY_SIZE), aggregate)
            table.insert(1, "half", name)
            table.insert(2, "from", str(part["date"].min().date()))
            table.insert(3, "to", str(part["date"].max().date()))
            half_rows.append(table.drop(columns=["significant"]))
    halves_table = pd.concat(half_rows, ignore_index=True)
    print("\n== gate 1: split halves (no bar; sign agreement with the full sample is the reading)")
    print(halves_table.to_string(index=False))
    sections.append(("gate 1: split halves by date (no bar)", _markdown(halves_table)))

    tercile_rows = []
    for aggregate, frame in frames.items():
        table = evo.tercile_table(frame)
        table.insert(0, "aggregate", aggregate)
        tercile_rows.append(table)
    terciles = pd.concat(tercile_rows, ignore_index=True)
    print("\n== gate 2: size terciles of the headline tests at 63 days (0 = smallest; no bar)")
    print(terciles.to_string(index=False))
    sections.append(("gate 2: size terciles, headline tests at 63 days (no bar)", _markdown(terciles)))

    ledger_lines = []
    for aggregate, frame in frames.items():
        ledger = evo.out_of_sample(frame)
        ready = {h: evo.ledger_ready(ledger, horizon=h) for h in evo.HORIZONS}
        dates = {h: int(ledger[ledger[f"ret_adj_{h}"].notna()]["date"].nunique()) for h in evo.HORIZONS}
        line = (f"{aggregate}: periods from {evo.OOS_FIRST_PERIOD}: {ledger['date'].nunique()} usable dates; "
                + ", ".join(f"{h}d returns on {dates[h]} (needs {evo.OOS_MIN_DATES})" for h in evo.HORIZONS))
        print(f"\n== out-of-sample ledger, {line}")
        ledger_lines.append(line)
        horizons = tuple(h for h in evo.HORIZONS if ready[h])
        if horizons:
            table = _table(evo.run_spec(ledger, evo.PRIMARY, size=evo.PRIMARY_SIZE, horizons=horizons), aggregate)
            print(table.to_string(index=False))
            sections.append((f"out-of-sample ledger, {aggregate} ({len(horizons) * evo.PRIMARY_SIZE // len(evo.HORIZONS)} tests at the family bar)", _markdown(table)))
    sections.append(("out-of-sample ledger", "\n".join(f"- {line}" for line in ledger_lines)))

    if args.out:
        lines = [
            "# EVAL-003 run (positioning.evaluation_ownership)",
            "",
            f"Run {time.strftime('%Y-%m-%d %H:%M')}; panel {len(panel):,} ladder rows over both aggregates, "
            f"{panel['symbol'].nunique():,} securities, periods {panel['date'].min().date()} to {panel['date'].max().date()}.",
            "",
        ]
        for head, body in sections:
            lines += [f"## {head}", "", body, ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
