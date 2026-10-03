"""Run the pre-registered register evaluation (EVAL-004, positioning.evaluation_register).

    .venv\\Scripts\\python.exe scripts\\register_eval.py [--market uk] [--out REPORT.md]

Reads <positioning root>/registers/<market>/issuers.parquet (scripts/register_panel.py);
writes nothing to the data root.
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
from positioning import evaluation_register as er  # noqa: E402
from positioning import register_panel  # noqa: E402
from positioning.config import positioning_root  # noqa: E402


def _markdown(table: pd.DataFrame) -> str:
    cols = list(table.columns)
    rows = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    rows += ["| " + " | ".join(str(v) for v in r) + " |" for r in table.itertuples(index=False)]
    return "\n".join(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--market", default="uk")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()
    t0 = time.time()
    directory = positioning_root() / register_panel.SUBDIR / args.market
    panel = pd.read_parquet(directory / "issuers.parquet")
    funds = pd.read_parquet(directory / "funds.parquet")
    panel["date"] = pd.to_datetime(panel["date"])
    frame = er.add_features(panel)
    obs = er.observations(frame, sample_end=er.SAMPLE_ENDS.get(args.market, None))
    print(f"panel {len(panel):,} issuer-days, {panel['isin'].nunique():,} issuers; observations {len(obs):,} over {obs['date'].nunique()} weeks "
          f"{obs['date'].min().date()} to {obs['date'].max().date()} ({time.time() - t0:.0f}s)")
    coverage = pd.DataFrame({
        "names per week (median)": [int(obs.groupby("date").size().median())],
        "has_flow_21": [round(float(obs["flow_21"].notna().mean()), 3)],
        "has_z_level": [round(float(obs["z_level"].notna().mean()), 3)],
        "has_factors": [round(float(obs["momentum_12_1"].notna().mean()), 3)],
        "seed share of fund rows": [round(float(funds["seed_share"].mean()), 3)],
        "late fund rows": [round(float(funds["late"].mean()), 3)],
    })
    print(coverage.T.to_string(header=False))
    pd.set_option("display.width", 220)
    pd.set_option("display.max_rows", 200)
    sections: list[tuple[str, str]] = []
    runs = [("pre-registered family", {}), ("post-hoc extension: six-month momentum in the cleaning", {"cleanings": er.EXTENDED_CLEANINGS})]
    if int(obs.groupby("date").size().median()) < er.MIN_NAMES:
        runs.append((f"post-hoc extension: dates with at least {er.MIN_NAMES // 2} names (the cross-section is thin)", {"min_names": er.MIN_NAMES // 2}))
    for label, kwargs in runs:
        results = er.run_family(obs, **kwargs)
        table = ev.results_table(results)
        table["p"] = table["p"].map(lambda v: f"{v:.2e}")
        size = results[0].family_size
        sig = [r for r in results if r.significant]
        head = f"EVAL-004 {args.market}, {label}: {size} tests, Bonferroni p < {0.05 / size:.1e}; {len(sig)} clear it, {sum(r.sign_agrees for r in sig)} with SPEC's sign"
        print(f"\n== {head}")
        print(table.to_string(index=False))
        sections.append((head, _markdown(table)))
    half_rows = []
    for name, part in zip(("first half", "second half"), er.halves(obs)):
        table = ev.results_table(er.run_family(part, features=("level", "profit_pct"), family_size=er.FAMILY_SIZE))
        table.insert(0, "half", name)
        table.insert(1, "from", str(part["date"].min().date()))
        table.insert(2, "to", str(part["date"].max().date()))
        table["p"] = table["p"].map(lambda v: f"{v:.2e}")
        half_rows.append(table.drop(columns=["significant"]))
    halves_table = pd.concat(half_rows, ignore_index=True)
    print("\n== gate: split halves for level and profit_pct (no bar; sign agreement is the reading)")
    print(halves_table.to_string(index=False))
    sections.append(("gate: split halves for level and profit_pct (no bar)", _markdown(halves_table)))
    if args.out:
        lines = [f"# EVAL-004 run ({args.market}, positioning.evaluation_register)", "",
                 f"Run {time.strftime('%Y-%m-%d %H:%M')}; panel {len(panel):,} issuer-days over {panel['isin'].nunique():,} issuers; "
                 f"{len(obs):,} weekly observations, {obs['date'].nunique()} weeks {obs['date'].min().date()} to {obs['date'].max().date()}.", "",
                 "Coverage:", "", _markdown(coverage.T.reset_index().rename(columns={"index": "measure", 0: "value"})), ""]
        for head, body in sections:
            lines += [f"## {head}", "", body, ""]
        args.out.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
