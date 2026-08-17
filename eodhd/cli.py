"""Unified command-line entry point for the datacli EODHD data lanes.

One front door for the whole workflow instead of remembering a dozen
``fetch_eodhd_*.py`` scripts. Everything is driven by the shared registry in
``eodhd_datasets.py``, so a new lane wired in there is picked up by every
command automatically.

Usage:
    uv run python eodhd/cli.py <command> [options]
    uv run python eodhd/cli.py --help
    uv run python eodhd/cli.py refresh --help

Lifecycle (the order a new install goes through; ``--help`` prints the same):
    config   -> point at a data root; needs EODHD_API_KEY for anything that fetches
    refresh  -> download / top up (dry-run unless --run; hits the paid API)
    status / qc -> what we have, how fresh, what is broken
    reindex  -> rebuild the query catalog (describe / find read it)
    describe / find / rows / coverage / sql -> explore

Commands: see ``COMMAND_HELP`` (this docstring intentionally does not repeat it).
"""

from __future__ import annotations

import argparse
import difflib
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _render  # noqa: E402
from eodhd_datasets import LANES  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = Path(__file__).resolve().parent

PROG = "uv run python eodhd/cli.py"

# Commands that simply forward their arguments to an existing standalone script.
DELEGATED: dict[str, str] = {
    "probe": "probe_eodhd_availability.py",
}

# Scripts behind the positional-scoped commands (status/qc take `[lane] [dataset]`).
STATUS_SCRIPT = "status_eodhd.py"
QC_SCRIPT = "report_eodhd_raw_quality.py"
NEWS_QC_SCRIPT = "report_news_quality.py"
# Datasets QC can drill into (mirrors report_eodhd_raw_quality.py's --dataset).
QC_DATASETS = ("prices", "dividends", "splits")

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
    ("sql", "Raw DuckDB query over the dataset views (incl. news)"),
    ("qc", "Run raw-data quality checks (qc [lane] [dataset])"),
    ("probe", "Probe ad-hoc ticker availability (paid API; caches under probe_cache/)"),
    ("lanes", "List registered lanes, datasets, and their fetchers"),
    ("config", "Show/edit config (config set data-root <path>)"),
    ("schema", "Declared schema version + drift vs on-disk data"),
    ("reindex", "(Re)build the fast query catalog after new data"),
]

# The lifecycle a fresh install walks through; each stage names its commands.
# ``$`` marks stages that hit the paid EODHD API (need EODHD_API_KEY); everything
# else runs offline against the local snapshot.
LIFECYCLE: list[tuple[str, str, bool]] = [
    ("1. setup", "config set data-root, set EODHD_API_KEY, lanes, schema", False),
    (
        "2. first fill",
        "refresh <lane> --datasets fundamentals --run, then refresh --run "
        "(see eodhd/README.md 'First fill'; hours, ~1 day of quota)",
        True,
    ),
    (
        "3. routine",
        "refresh --fast --run (daily top-up incl. news), "
        "refresh --datasets fundamentals --run (weekly)",
        True,
    ),
    ("4. verify", "status [lane], qc [lane] [dataset]", False),
    (
        "5. index",
        "reindex  (describe/find read the catalog -- run after every fetch)",
        False,
    ),
    ("6. explore", "describe, find, rows, coverage, sql", False),
]

KNOWN_KINDS = (
    "prices",
    "dividends",
    "splits",
    "fundamentals",
    "news",
    "news_daily",
    "issuer_map",
    "news_issuer_daily",
)
# news rides the default refresh: its registry args cap each run to a bounded
# top-up (newest days first), so a routine refresh never turns into a backfill.
# The one-off backfill is an explicit `python eodhd/fetch_eodhd_news.py`.
# news_daily is the local, seconds-cheap panel build that follows the news top-up.
DEFAULT_KINDS = (
    "prices",
    "dividends",
    "splits",
    "news",
    "news_daily",
    "issuer_map",
    "news_issuer_daily",
)
# Kinds whose fetchers accept the incremental passthrough flags (--to/--limit/...).
# fundamentals is refreshed via its own --update mode and takes different flags;
# news is a day-keyed crawl that must never receive --full-refresh/--tickers.
INCREMENTAL_KINDS = frozenset({"prices", "dividends", "splits"})


# --------------------------------------------------------------------------- #
# top-level help
# --------------------------------------------------------------------------- #
def top_help() -> str:
    lines = [
        "eodhd - unified CLI for the datacli EODHD data lanes",
        "",
        f"Usage:\n  {PROG} <command> [options]",
        "",
        "Commands:",
    ]
    width = max(len(name) for name, _ in COMMAND_HELP)
    for name, desc in COMMAND_HELP:
        lines.append(f"  {name:<{width}}  {desc}")
    lines += ["", "Lifecycle ($ = hits the paid EODHD API, needs EODHD_API_KEY):"]
    stage_w = max(len(stage) for stage, _, _ in LIFECYCLE)
    for stage, what, paid in LIFECYCLE:
        lines.append(f"  {stage:<{stage_w}}  {'$' if paid else ' '} {what}")
    lines += [
        "",
        f"Run '{PROG} <command> --help' for command-specific options.",
        "",
        "Examples:",
        f"  {PROG} config set data-root D:\\data\\raw\\eodhd",
        f"  {PROG} status                 # as-of table for every dataset",
        f"  {PROG} status --write         # also refresh <data-root>/STATUS.md + .json",
        f"  {PROG} lanes                  # what lanes/datasets exist",
        f"  {PROG} refresh                # show the refresh plan (no fetch)",
        f"  {PROG} refresh --run          # execute: prices + events + news top-up, all lanes",
        f"  {PROG} refresh --fast --run   # same via bulk endpoints (minutes); top-up only",
        f"  {PROG} refresh us_common --datasets fundamentals --run",
        f"  {PROG} reindex                # then: describe AAPL.US / find AAPL / sql ...",
        f"  {PROG} probe AAPL MSFT NVDA   # ad-hoc availability check ($)",
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

    from rich.text import Text

    console = _render.make_console()
    table = _render.minimal_table(title=f"EODHD lanes ({len(LANES)})")
    table.add_column("lane", no_wrap=True, min_width=12)
    table.add_column("region / class", no_wrap=True, style="dim")
    table.add_column("dataset", no_wrap=True, min_width=16)
    table.add_column("fetcher", overflow="fold")

    for lane in LANES.values():
        region = f"{lane.region} / {lane.asset_class}"
        table.add_row(
            lane.name,
            region,
            Text("universe", style="dim"),
            Text(lane.universe_source(), style="" if lane.universe_fetcher else "dim"),
        )
        for ds in lane.datasets:
            fetcher = ds.fetcher or "(no fetcher)"
            extra = (" " + " ".join(ds.fetcher_args)) if ds.fetcher_args else ""
            snapshot = "  [snapshot]" if ds.state is None else ""
            dataset = Text()
            dataset.append_text(_render.kind_dot(ds.kind))
            dataset.append(f" {ds.display}")
            table.add_row(
                "",
                "",
                dataset,
                Text(f"{fetcher}{extra}", style="dim") + Text(snapshot, style="cyan"),
            )
        table.add_section()
    console.print(table)
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
    local: bool = False  # a local build: no API calls, no quota

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
    fundamentals -> news, following registry declaration order. ``passthrough``
    args are appended only to the incremental per-ticker fetchers
    (prices/dividends/splits) — never to the universe step, the fundamentals
    ``--update`` refresh, or the day-keyed news crawl, which take different flags.
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
        lane_steps: list[Step] = []
        for ds in matching:
            assert ds.fetcher is not None  # guaranteed by `matching`
            step_args = list(ds.fetcher_args)
            if ds.kind in INCREMENTAL_KINDS:  # these fetchers accept passthrough flags
                step_args += passthrough
            lane_steps.append(
                Step(name, ds.kind, ds.fetcher, step_args, local=ds.local)
            )
        if lane.bootstrap_missing():
            # First fill of a common-stock lane: the per-ticker fetchers need
            # the coverage file the fundamentals stage writes. Run fundamentals
            # first when selected; otherwise those steps cannot run yet.
            fund = [s for s in lane_steps if s.kind == "fundamentals"]
            incr = [s for s in lane_steps if s.kind in INCREMENTAL_KINDS]
            rest = [s for s in lane_steps if s not in fund and s not in incr]
            lane_steps = (fund + incr + rest) if fund else rest
        steps.extend(lane_steps)
    return steps


def bootstrap_notes(lane_names: list[str], kinds: set[str]) -> list[str]:
    """Explain, per lane, what :func:`build_refresh_plan` did about a missing
    bootstrap file — so a first fill never fails silently at step 1."""
    notes: list[str] = []
    for name in lane_names:
        lane = LANES[name]
        if not lane.bootstrap_missing() or not (kinds & INCREMENTAL_KINDS):
            continue
        selected = ", ".join(k for k in KNOWN_KINDS if k in kinds & INCREMENTAL_KINDS)
        if "fundamentals" in kinds:
            notes.append(
                f"{name}: {lane.bootstrap_file} not found -> fundamentals runs "
                f"first (it writes it), then {selected}"
            )
        else:
            notes.append(
                f"{name}: {selected} skipped -- {lane.bootstrap_file} not found; "
                f"first fill: {PROG} refresh {name} --datasets fundamentals --run"
            )
    return notes


def cmd_refresh(argv: list[str]) -> int:
    """Download the latest data across lanes (incremental; dry-run unless --run)."""
    parser = argparse.ArgumentParser(
        prog=f"{PROG} refresh",
        description="Download the latest EODHD data. Prints the plan and does "
        "nothing unless --run is given (it hits the paid EODHD API; needs "
        "EODHD_API_KEY). Per lane the steps run universe -> prices -> dividends "
        "-> splits -> fundamentals -> news. First fill of a common-stock lane "
        "must start with --datasets fundamentals (it writes the coverage file "
        "the price/event fetchers need); see eodhd/README.md 'First fill'.",
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
        f"(default: {','.join(DEFAULT_KINDS)}). news is a bounded top-up here "
        "(newest days first, capped by the registry); the one-off backfill is "
        "'python eodhd/fetch_eodhd_news.py'.",
    )
    parser.add_argument(
        "--with-fundamentals",
        action="store_true",
        help="Also run the incremental fundamentals refresh (--update: firms that "
        "reported since the last pull, plus new firms). Same as adding "
        "'fundamentals' to --datasets. A full rebuild is the fetcher script's "
        "own --full-refresh, not exposed here.",
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
        help="Top-up via bulk end-of-day endpoints for prices/dividends/splits "
        "(one call per exchange -> minutes instead of hours; only lanes that "
        "already have state); the news top-up still runs afterwards if selected. "
        "Not for a first fill. Incompatible with --full-refresh/--to/--limit/"
        "--tickers.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=None,
        help="With --fast only: max days back to fill (default 7).",
    )
    parser.add_argument(
        "--keep-going", action="store_true", help="Continue after a fetcher fails."
    )
    parser.add_argument(
        "--full-refresh",
        action="store_true",
        help="Forward --full-refresh to the prices/dividends/splits fetchers "
        "(never to fundamentals or news).",
    )
    parser.add_argument(
        "--to", default="", help="Forward --to YYYY-MM-DD (prices/dividends/splits)."
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Forward --limit N (prices/dividends/splits).",
    )
    parser.add_argument(
        "--tickers",
        nargs="*",
        default=[],
        help="Forward --tickers ... (prices/dividends/splits).",
    )
    args = parser.parse_args(argv)

    lane_names = args.lanes or list(LANES)
    unknown = [name for name in lane_names if name not in LANES]
    if unknown:
        _bad_choice("lane", unknown[0], tuple(LANES))
        return 2

    kinds = {k.strip() for k in args.datasets.split(",") if k.strip()}
    if args.with_fundamentals:
        kinds.add("fundamentals")
    bad = kinds - set(KNOWN_KINDS)
    if bad:
        _bad_choice("dataset kind", sorted(bad)[0], KNOWN_KINDS)
        return 2

    if args.fast:
        bulk_kinds = sorted(kinds & INCREMENTAL_KINDS)
        if not bulk_kinds:
            parser.error("--fast supports prices/dividends/splits only; none selected")
        ignored = [
            flag
            for flag, given in (
                ("--full-refresh", args.full_refresh),
                ("--to", bool(args.to)),
                ("--limit", args.limit is not None),
                ("--tickers", bool(args.tickers)),
                ("--no-universe", args.no_universe),
            )
            if given
        ]
        if ignored:
            parser.error(
                f"{', '.join(ignored)} cannot be combined with --fast (the bulk "
                "path has no per-ticker window); drop --fast for a targeted rerun"
            )
        if "fundamentals" in kinds:
            print(
                "note: --fast covers prices/dividends/splits; refresh fundamentals via "
                "'refresh --datasets fundamentals --run'\n"
            )
        # Only lanes with a bulk-refreshable dataset go to the bulk script; the
        # news lane (day-keyed) is handled by the follow-up plan below.
        bulk_lanes = [
            n
            for n in lane_names
            if any(ds.kind in INCREMENTAL_KINDS for ds in LANES[n].datasets)
        ]
        forwarded = [
            *bulk_lanes,
            "--kinds",
            ",".join(bulk_kinds),
            "--days",
            str(args.days if args.days is not None else 7),
        ]
        if args.run:
            forwarded.append("--run")
        rc = 0
        if bulk_lanes:
            rc = delegate("fetch_eodhd_bulk.py", forwarded, prog="refresh --fast")
        # Kinds the bulk endpoint cannot serve but that are cheap, self-bounded
        # crawls (news) still run here, so --fast keeps every default dataset
        # current rather than silently skipping the non-ticker lanes.
        extra_kinds = kinds - INCREMENTAL_KINDS - {"fundamentals"}
        extra = build_refresh_plan(
            lane_names, kinds=extra_kinds, with_universe=False, passthrough=[]
        )
        if extra:
            rc_extra = _print_and_run_steps(
                extra, run=args.run, keep_going=args.keep_going
            )
            rc = rc or rc_extra
        return rc

    if args.days is not None:
        parser.error("--days only applies with --fast")

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
    for note in bootstrap_notes(lane_names, kinds):
        print(f"note: {note}")

    if not steps:
        if bootstrap_notes(lane_names, kinds):
            print(
                "Nothing runnable yet -- see the note(s) above (run the "
                "fundamentals stage first, or add --with-fundamentals)."
            )
            return 0
        print("Nothing to do (no matching datasets for the selected lanes/kinds).")
        opt_in = sorted(
            {
                ds.kind
                for name in lane_names
                for ds in LANES[name].datasets
                if ds.kind in KNOWN_KINDS and ds.kind not in DEFAULT_KINDS
            }
            - kinds
        )
        if opt_in:
            print(f"hint: opt in with --datasets {','.join(opt_in)}")
        return 0

    return _print_and_run_steps(steps, run=args.run, keep_going=args.keep_going)


def _print_and_run_steps(steps: list[Step], *, run: bool, keep_going: bool) -> int:
    """Show the plan table; execute the steps in order when ``run`` is set."""
    from rich.text import Text

    console = _render.make_console()
    mode = "RUN" if run else "DRY-RUN"
    mode_style = "bold red" if run else "yellow"
    title = Text("refresh plan  ", style="bold")
    title.append(f"[{mode}]", style=mode_style)
    title.append(f"  {len(steps)} step(s)", style="dim")
    table = _render.minimal_table(title=title)  # type: ignore[arg-type]
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("lane", no_wrap=True, min_width=12)
    table.add_column("dataset", no_wrap=True, min_width=14)
    # The command is the whole point of a dry-run plan: fold, never truncate.
    table.add_column("command", overflow="fold", style="dim")
    prev_lane: str | None = None
    for i, step in enumerate(steps, 1):
        kind = Text()
        kind.append_text(_render.kind_dot(step.kind))
        kind.append(f" {step.kind}")
        command = Text(step.display())
        if step.local:
            command.append("  (local, no API)", style="green")
        table.add_row(
            str(i),
            "" if step.lane == prev_lane else step.lane,
            kind,
            command,
        )
        prev_lane = step.lane
    console.print(table)
    n_api = sum(1 for s in steps if not s.local)
    console.print(
        Text(
            f"{n_api} of {len(steps)} step(s) hit the paid EODHD API"
            + (f"; {len(steps) - n_api} local" if n_api < len(steps) else ""),
            style="dim",
        )
    )

    if not run:
        console.print(
            Text(
                "dry-run only — re-run with --run to execute (hits the paid EODHD API).",
                style="dim",
            )
        )
        return 0

    failures: list[tuple[Step, int]] = []
    completed = 0
    for i, step in enumerate(steps, 1):
        print(f"\n===== [{i}/{len(steps)}] {step.lane} :: {step.kind} =====")
        print(f"    {step.display()}")
        rc = subprocess.run(step.argv(), cwd=str(REPO_ROOT)).returncode
        if rc != 0:
            failures.append((step, rc))
            print(f"    -> FAILED (exit {rc})")
            if not keep_going:
                print("Stopping (use --keep-going to continue past failures).")
                break
        else:
            completed += 1

    print(
        f"\nRefresh finished: {completed} ok, {len(failures)} failed, "
        f"{len(steps) - completed - len(failures)} not run."
    )
    for step, rc in failures:
        print(f"  FAILED: {step.lane} {step.kind} (exit {rc})")
    return 1 if failures else 0


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
#: ``config set/get`` keys -> (datacli.toml section, key).
CONFIG_KEYS: dict[str, tuple[str, str]] = {
    "data-root": ("eodhd", "data_root"),
    "sync-backend": ("sync", "backend"),
    "sync-remote-root": ("sync", "remote_root"),
    "sync-gdrive-secrets": ("sync", "gdrive_client_secrets"),
    "sync-local-dest": ("sync", "local_dest"),
}


def cmd_config(argv: list[str]) -> int:
    """Show or edit datacli config: data root, backup (sync) settings; shows
    whether an EODHD API key resolves and where the config file lives."""
    import config as cfg  # eodhd/config.py

    keys = ", ".join(CONFIG_KEYS)
    parser = argparse.ArgumentParser(
        prog=f"{PROG} config",
        description=(
            "Show or edit datacli.toml (git-ignored, at the repo root). "
            f"Keys: {keys}. The EODHD API key is NOT stored here -- set the "
            "EODHD_API_KEY environment variable (or see the README for the "
            "file fallbacks); 'config' shows whether it resolves."
        ),
    )
    sub = parser.add_subparsers(dest="action")
    sub.add_parser("show", help="Print the resolved settings (default).")
    g = sub.add_parser("get", help="Print one value.")
    g.add_argument("key", help=f"One of: {keys}")
    s = sub.add_parser("set", help="Persist one value into datacli.toml.")
    s.add_argument("key", help=f"One of: {keys}")
    s.add_argument("value")
    args = parser.parse_args(argv or ["show"])

    keymap = CONFIG_KEYS

    if args.action in ("set", "get") and args.key not in keymap:
        _bad_choice("config key", args.key, tuple(keymap))
        return 2
    if args.action == "set":
        section, key = keymap[args.key]
        path = cfg.set_value(section, key, args.value)
        print(f"set {args.key} = {args.value}\n  ({path})")
        return 0
    if args.action == "get":
        section, key = keymap[args.key]
        print(cfg.get(section, key, "") or "")
        return 0

    from rich.text import Text

    root, source = cfg.eodhd_data_root()
    exists_ok = root.exists()
    try:
        from fetch_eodhd_eu_fundamentals import _get_api_key

        api = _get_api_key()
        masked = ("****" + api[-4:]) if len(api) >= 4 else "set"
        api_set = True
    except Exception:
        masked = "NOT SET"
        api_set = False

    console = _render.make_console()
    table = _render.boxed_table(
        title="datacli config (eodhd)",
        caption=f"set with:  config set <key> <value>   keys: {keys}",
    )
    table.add_column("setting", style="cyan", no_wrap=True)
    table.add_column("value")

    root_val = Text(str(root))
    root_val.append(f"  ({source}, ", style="dim")
    root_val.append(
        "exists" if exists_ok else "MISSING", style="green" if exists_ok else "red"
    )
    root_val.append(")", style="dim")
    table.add_row("data-root", root_val)
    table.add_row("config file", Text(str(cfg.CONFIG_PATH), style="dim"))
    table.add_row("EODHD_API_KEY", Text(masked, style="green" if api_set else "red"))
    table.add_row("lanes", Text(", ".join(LANES), style="dim"))
    sync = cfg.section("sync")
    sync_val = Text(str(sync.get("backend") or "gdrive"))
    sync_val.append(
        (
            f"  -> {sync.get('remote_root') or 'datacli/eodhd'}"
            if (sync.get("backend") or "gdrive") == "gdrive"
            else f"  -> {sync.get('local_dest') or '(sync-local-dest not set)'}"
        ),
        style="dim",
    )
    table.add_row("sync backend", sync_val)
    console.print(table)
    return 0


# --------------------------------------------------------------------------- #
# delegated commands
# --------------------------------------------------------------------------- #
def delegate(script: str, argv: list[str], *, prog: str | None = None) -> int:
    """Forward a subcommand to an existing standalone script (incl. --help).

    ``prog`` is exported as ``DATACLI_PROG`` so the script's argparse usage reads
    ``eodhd status ...`` rather than ``status_eodhd.py ...``.
    """
    env = dict(os.environ)
    if prog:
        env["DATACLI_PROG"] = f"{PROG} {prog}"
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *argv],
        cwd=str(REPO_ROOT),
        env=env,
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


def _bad_choice(kind: str, value: str, choices: tuple[str, ...]) -> None:
    """Print a friendly, styled error with a 'did you mean' hint (not argparse's
    full-usage dump), so a shell typo like ``qc us_common divivdends`` reads well.
    """
    from rich.text import Text

    console = _render.make_console()
    msg = Text()
    msg.append("unknown ", style="red")
    msg.append(kind, style="red")
    msg.append(f" '{value}'", style="bold red")
    near = difflib.get_close_matches(value, choices, n=1)
    if near:
        msg.append("  — did you mean ", style="dim")
        msg.append(near[0], style="green")
        msg.append("?", style="dim")
    console.print(msg)
    console.print(Text(f"choices: {', '.join(choices)}", style="dim"))


def cmd_status(argv: list[str]) -> int:
    """`status [lane] [--flags]` -> status_eodhd.py with --lane translated."""
    pos, rest = _leading_positionals(argv)
    lanes = (*LANES, "all")
    if pos and pos[0] not in lanes:
        _bad_choice("lane", pos[0], lanes)
        return 2
    forwarded = (["--lane", pos[0]] if pos else []) + rest
    return delegate(STATUS_SCRIPT, forwarded, prog="status")


def cmd_qc(argv: list[str]) -> int:
    """`qc [lane] [dataset] [--flags]` -> report_eodhd_raw_quality.py.

    First positional is the lane, second the dataset drill-down; both optional.
    Validated up front so a typo yields a friendly hint, not an argparse dump.
    """
    pos, rest = _leading_positionals(argv)
    # The price QC engine audits price-bearing lanes; the news lane has its own
    # corpus-hygiene report (report_news_quality.py).
    price_lanes = tuple(
        n
        for n, lane in LANES.items()
        if any(ds.kind == "prices" for ds in lane.datasets)
    )
    lanes = (*price_lanes, *(("news",) if "news" in LANES else ()), "all")
    if pos and pos[0] not in lanes:
        _bad_choice("lane", pos[0], lanes)
        return 2
    if pos and pos[0] == "news":
        if len(pos) > 1:
            _bad_choice("dataset", pos[1], ())
            return 2
        return delegate(NEWS_QC_SCRIPT, rest, prog="qc news")
    if len(pos) > 1 and pos[1] not in QC_DATASETS:
        _bad_choice("dataset", pos[1], QC_DATASETS)
        return 2
    forwarded: list[str] = []
    if pos:
        forwarded += ["--lane", pos[0]]
    if len(pos) > 1:
        forwarded += ["--dataset", pos[1]]
    forwarded += rest
    return delegate(QC_SCRIPT, forwarded, prog="qc")


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
        from scheduler.commands import direct_mutation_lock

        with direct_mutation_lock("eodhd", "refresh", rest):
            return cmd_refresh(rest)
    if command == "config":
        return cmd_config(rest)
    if command == "status":
        return cmd_status(rest)
    if command == "qc":
        return cmd_qc(rest)
    if command in EXPLORE_COMMANDS:
        if command == "reindex":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("eodhd", "reindex", rest):
                return delegate("explore_eodhd.py", [command, *rest], prog=command)
        return delegate("explore_eodhd.py", [command, *rest], prog=command)
    if command in DELEGATED:
        return delegate(DELEGATED[command], rest, prog=command)

    print(f"unknown command: {command!r}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
