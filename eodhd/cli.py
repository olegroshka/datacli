"""Unified command-line entry point for the btest EODHD data lanes.

One front door for the whole workflow — status, refresh, QC, probing — instead of
remembering a dozen ``fetch_eodhd_*.py`` scripts. Everything is driven by the
shared registry in ``eodhd_datasets.py``, so a new lane wired in there is picked
up by every command automatically.

Usage:
    uv run python eodhd/cli.py <command> [options]
    uv run python eodhd/cli.py --help
    uv run python eodhd/cli.py refresh --help

Commands:
    status    Show what data we have and as of when (all lanes).
    refresh   Download the latest data across lanes (dry-run unless --run).
    qc        Run raw-data quality checks.
    probe     Probe ad-hoc ticker availability (read-only; no writes).
    lanes     List registered lanes, datasets, and their fetchers.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from eodhd_datasets import LANES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parent

PROG = "uv run python eodhd/cli.py"

# Commands that simply forward their arguments to an existing standalone script.
DELEGATED: dict[str, str] = {
    "probe": "probe_eodhd_availability.py",
}

# Scripts behind the positional-scoped commands (status/qc take `[lane] [dataset]`).
STATUS_SCRIPT = "status_eodhd.py"
QC_SCRIPT = "report_eodhd_raw_quality.py"

# Entity-centric exploration verbs, all handled by explore_eodhd.py (DuckDB).
EXPLORE_COMMANDS = ("describe", "find", "rows", "coverage", "sql", "schema", "reindex")

# One-line descriptions for the top-level help, in display order.
COMMAND_HELP: list[tuple[str, str]] = [
    ("status", "Show what data we have and as of when (status [lane])"),
    ("refresh", "Download the latest data across lanes (dry-run unless --run)"),
    ("describe", "Everything about one ticker, across datasets"),
    ("find", "Locate a ticker (lane / exchange / datasets)"),
    ("rows", "Show the actual rows for a ticker in a dataset"),
    ("coverage", "Do the datasets cover a ticker equally?"),
    ("sql", "Raw DuckDB query over the datasets"),
    ("qc", "Run raw-data quality checks (qc [lane] [dataset])"),
    ("probe", "Probe ad-hoc ticker availability (read-only; no writes)"),
    ("lanes", "List registered lanes, datasets, and their fetchers"),
    ("config", "Show/edit config (config set data-root <path>)"),
    ("schema", "Declared schema version + drift vs on-disk data"),
    ("reindex", "(Re)build the fast query catalog after new data"),
]

KNOWN_KINDS = ("prices", "dividends", "splits", "fundamentals")
DEFAULT_KINDS = ("prices", "dividends", "splits")
# Kinds whose fetchers accept the incremental passthrough flags (--to/--limit/...).
# fundamentals is refreshed via its own --update mode and takes different flags.
INCREMENTAL_KINDS = frozenset({"prices", "dividends", "splits"})


# --------------------------------------------------------------------------- #
# top-level help
# --------------------------------------------------------------------------- #
def top_help() -> str:
    lines = [
        "eodhd - unified CLI for the btest EODHD data lanes",
        "",
        f"Usage:\n  {PROG} <command> [options]",
        "",
        "Commands:",
    ]
    width = max(len(name) for name, _ in COMMAND_HELP)
    for name, desc in COMMAND_HELP:
        lines.append(f"  {name:<{width}}  {desc}")
    lines += [
        "",
        f"Run '{PROG} <command> --help' for command-specific options.",
        "",
        "Examples:",
        f"  {PROG} status                 # as-of table for every dataset",
        f"  {PROG} status --write         # also refresh STATUS.md / STATUS.json",
        f"  {PROG} lanes                  # what lanes/datasets exist",
        f"  {PROG} refresh                # show the refresh plan (no fetch)",
        f"  {PROG} refresh --run          # execute: prices + events, all lanes",
        f"  {PROG} refresh us_common --run",
        f"  {PROG} refresh --with-fundamentals --run",
        f"  {PROG} probe AAPL MSFT NVDA   # ad-hoc availability check",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# `lanes`
# --------------------------------------------------------------------------- #
def cmd_lanes(argv: list[str]) -> int:
    """List every registered lane, its datasets, and the fetcher for each."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} lanes", description=cmd_lanes.__doc__
    )
    parser.parse_args(argv)

    print(f"EODHD lanes ({len(LANES)}):\n")
    for lane in LANES.values():
        print(f"{lane.name}   [{lane.region} / {lane.asset_class}]")
        if lane.universe_fetcher:
            print(f"    universe      {lane.universe_fetcher}")
        else:
            print("    universe      (derived from another stage)")
        for ds in lane.datasets:
            snapshot = "  [snapshot]" if ds.state is None else ""
            fetcher = ds.fetcher or "(no fetcher)"
            extra = (" " + " ".join(ds.fetcher_args)) if ds.fetcher_args else ""
            print(f"    {ds.display:<15} {fetcher}{extra}{snapshot}")
        print()
    return 0


# --------------------------------------------------------------------------- #
# `refresh`
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Step:
    """One fetcher invocation in a refresh plan."""

    lane: str
    kind: str
    script: str
    args: list[str] = field(default_factory=list)

    def display(self) -> str:
        tail = (" " + " ".join(self.args)) if self.args else ""
        return f"python eodhd/{self.script}{tail}"

    def argv(self) -> list[str]:
        return [sys.executable, str(SCRIPTS_DIR / self.script), *self.args]


def build_refresh_plan(
    lane_names: list[str],
    *,
    kinds: set[str],
    with_universe: bool,
    passthrough: list[str],
) -> list[Step]:
    """Assemble the ordered fetcher steps for a refresh.

    Order per lane: universe (if any) -> prices -> dividends -> splits ->
    fundamentals, following registry declaration order. ``passthrough`` args are
    appended only to the incremental (state-backed) fetchers — never to the
    universe step or the snapshot fundamentals rebuild, which take different flags.
    """
    steps: list[Step] = []
    for name in lane_names:
        lane = LANES[name]
        matching = [
            ds for ds in lane.datasets if ds.kind in kinds and ds.fetcher is not None
        ]
        if not matching:
            continue  # nothing selected here -> skip the lane (and its universe step)
        if with_universe and lane.universe_fetcher:
            steps.append(
                Step(
                    name,
                    "universe",
                    lane.universe_fetcher,
                    list(lane.universe_fetcher_args),
                )
            )
        for ds in matching:
            assert ds.fetcher is not None  # guaranteed by `matching`
            step_args = list(ds.fetcher_args)
            if ds.kind in INCREMENTAL_KINDS:  # these fetchers accept passthrough flags
                step_args += passthrough
            steps.append(Step(name, ds.kind, ds.fetcher, step_args))
    return steps


def cmd_refresh(argv: list[str]) -> int:
    """Download the latest data across lanes (incremental; dry-run unless --run)."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} refresh",
        description="Download the latest EODHD data. Prints the plan and does "
        "nothing unless --run is given (it hits a paid API).",
    )
    parser.add_argument(
        "lanes",
        nargs="*",
        metavar="LANE",
        help=f"Lanes to refresh (default: all). Choices: {', '.join(LANES)}",
    )
    parser.add_argument(
        "--datasets",
        default=",".join(DEFAULT_KINDS),
        help=f"Comma list of dataset kinds. Choices: {', '.join(KNOWN_KINDS)} "
        f"(default: {','.join(DEFAULT_KINDS)}).",
    )
    parser.add_argument(
        "--with-fundamentals",
        action="store_true",
        help="Also rebuild the fundamentals snapshot (heavy full re-pull).",
    )
    parser.add_argument(
        "--no-universe", action="store_true", help="Skip the universe refresh step."
    )
    parser.add_argument(
        "--run", action="store_true", help="Actually execute (default: dry-run)."
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Use bulk end-of-day endpoints for prices/dividends/splits "
        "(one call per exchange -> minutes instead of hours).",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="With --fast, max days back to fill (default 7).",
    )
    parser.add_argument(
        "--keep-going", action="store_true", help="Continue after a fetcher fails."
    )
    parser.add_argument(
        "--full-refresh", action="store_true", help="Forward --full-refresh."
    )
    parser.add_argument("--to", default="", help="Forward --to YYYY-MM-DD.")
    parser.add_argument("--limit", type=int, default=None, help="Forward --limit N.")
    parser.add_argument(
        "--tickers", nargs="*", default=[], help="Forward --tickers ..."
    )
    args = parser.parse_args(argv)

    lane_names = args.lanes or list(LANES)
    unknown = [name for name in lane_names if name not in LANES]
    if unknown:
        parser.error(
            f"unknown lane(s): {', '.join(unknown)}. Choices: {', '.join(LANES)}"
        )

    kinds = {k.strip() for k in args.datasets.split(",") if k.strip()}
    if args.with_fundamentals:
        kinds.add("fundamentals")
    bad = kinds - set(KNOWN_KINDS)
    if bad:
        parser.error(f"unknown dataset kind(s): {', '.join(sorted(bad))}")

    if args.fast:
        bulk_kinds = sorted(kinds & INCREMENTAL_KINDS)
        if not bulk_kinds:
            parser.error("--fast supports prices/dividends/splits only; none selected")
        if "fundamentals" in kinds:
            print(
                "note: --fast covers prices/dividends/splits; refresh fundamentals via "
                "'refresh --datasets fundamentals --run'\n"
            )
        forwarded = [
            *lane_names,
            "--kinds",
            ",".join(bulk_kinds),
            "--days",
            str(args.days),
        ]
        if args.run:
            forwarded.append("--run")
        return delegate("fetch_eodhd_bulk.py", forwarded)

    passthrough: list[str] = []
    if args.full_refresh:
        passthrough.append("--full-refresh")
    if args.to:
        passthrough += ["--to", args.to]
    if args.limit is not None:
        passthrough += ["--limit", str(args.limit)]
    if args.tickers:
        passthrough += ["--tickers", *args.tickers]

    steps = build_refresh_plan(
        lane_names,
        kinds=kinds,
        with_universe=not args.no_universe,
        passthrough=passthrough,
    )

    if not steps:
        print("Nothing to do (no matching datasets for the selected lanes/kinds).")
        return 0

    mode = "RUN" if args.run else "DRY-RUN"
    print(f"Refresh plan ({mode}) - {len(steps)} step(s):\n")
    for i, step in enumerate(steps, 1):
        print(f"  {i:>2}. {step.lane:<16} {step.kind:<12} {step.display()}")

    if not args.run:
        print(
            f"\nDry-run only. Re-run with --run to execute (hits the paid EODHD API)."
        )
        return 0

    failures: list[tuple[Step, int]] = []
    for i, step in enumerate(steps, 1):
        print(f"\n===== [{i}/{len(steps)}] {step.lane} :: {step.kind} =====")
        print(f"    {step.display()}")
        rc = subprocess.run(step.argv(), cwd=str(REPO_ROOT)).returncode
        if rc != 0:
            failures.append((step, rc))
            print(f"    -> FAILED (exit {rc})")
            if not args.keep_going:
                print("Stopping (use --keep-going to continue past failures).")
                break

    print(
        f"\nRefresh finished: {len(steps) - len(failures)} ok, {len(failures)} failed."
    )
    for step, rc in failures:
        print(f"  FAILED: {step.lane} {step.kind} (exit {rc})")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def cmd_config(argv: list[str]) -> int:
    """Show or edit datacli config (eodhd data root, API key, lanes)."""
    import config as cfg  # eodhd/config.py

    parser = argparse.ArgumentParser(prog=f"{PROG} config")
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("show")
    g = sub.add_parser("get")
    g.add_argument("key")
    s = sub.add_parser("set")
    s.add_argument("key")
    s.add_argument("value")
    args = parser.parse_args(argv or ["show"])

    keymap = {"data-root": ("eodhd", "data_root")}

    if args.action == "set":
        if args.key not in keymap:
            parser.error(f"unknown key '{args.key}'. Settable: {', '.join(keymap)}")
        section, key = keymap[args.key]
        path = cfg.set_value(section, key, args.value)
        print(f"set {args.key} = {args.value}\n  ({path})")
        return 0
    if args.action == "get":
        if args.key not in keymap:
            parser.error(f"unknown key '{args.key}'. Known: {', '.join(keymap)}")
        section, key = keymap[args.key]
        print(cfg.get(section, key, "") or "")
        return 0

    root, source = cfg.eodhd_data_root()
    exists = "exists" if root.exists() else "MISSING"
    try:
        from fetch_eodhd_eu_fundamentals import _get_api_key

        api = _get_api_key()
        masked = ("****" + api[-4:]) if len(api) >= 4 else "set"
    except Exception:
        masked = "NOT SET"
    print("datacli config (eodhd):")
    print(f"  data-root      {root}  ({source}, {exists})")
    print(f"  config file    {cfg.CONFIG_PATH}")
    print(f"  EODHD_API_KEY  {masked}")
    print(f"  lanes          {', '.join(LANES)}")
    print("\nset with:  config set data-root <path>")
    return 0


# --------------------------------------------------------------------------- #
# delegated commands
# --------------------------------------------------------------------------- #
def delegate(script: str, argv: list[str]) -> int:
    """Forward a subcommand to an existing standalone script (incl. --help)."""
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *argv], cwd=str(REPO_ROOT)
    ).returncode


def _leading_positionals(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split off the leading non-flag tokens (before the first ``-x``).

    Lets ``qc us_common splits --as-of 2026-01-01`` read the lane/dataset as
    positionals while still forwarding any ``--flags`` untouched.
    """
    i = 0
    while i < len(argv) and not argv[i].startswith("-"):
        i += 1
    return argv[:i], argv[i:]


def cmd_status(argv: list[str]) -> int:
    """`status [lane] [--flags]` -> status_eodhd.py with --lane translated."""
    pos, rest = _leading_positionals(argv)
    forwarded = (["--lane", pos[0]] if pos else []) + rest
    return delegate(STATUS_SCRIPT, forwarded)


def cmd_qc(argv: list[str]) -> int:
    """`qc [lane] [dataset] [--flags]` -> report_eodhd_raw_quality.py.

    First positional is the lane, second the dataset drill-down; both optional.
    """
    pos, rest = _leading_positionals(argv)
    forwarded: list[str] = []
    if pos:
        forwarded += ["--lane", pos[0]]
    if len(pos) > 1:
        forwarded += ["--dataset", pos[1]]
    forwarded += rest
    return delegate(QC_SCRIPT, forwarded)


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(top_help())
        return 0

    command, rest = args[0], args[1:]
    if command == "lanes":
        return cmd_lanes(rest)
    if command == "refresh":
        return cmd_refresh(rest)
    if command == "config":
        return cmd_config(rest)
    if command == "status":
        return cmd_status(rest)
    if command == "qc":
        return cmd_qc(rest)
    if command in EXPLORE_COMMANDS:
        return delegate("explore_eodhd.py", [command, *rest])
    if command in DELEGATED:
        return delegate(DELEGATED[command], rest)

    print(f"unknown command: {command!r}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
