"""``positioning`` command group: status / build / qc for the derived datasets.

``build`` is a dry run unless ``--run``: it recomputes a ladder from the local
views (no network) and reports which dates are new or restated; ``--run``
writes them. ``--dataset`` picks the short ladder (default, over FINRA short
interest) or the long ladder (over SEC 13F holdings).
"""

from __future__ import annotations

import difflib
import json as jsonlib
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import positioning  # noqa: E402,F401  (puts eodhd/ on sys.path)
import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

from positioning import config as pos_config  # noqa: E402
from positioning import dataset  # noqa: E402

PROG = "positioning"

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What derived positioning data is on disk (bare `positioning` does this)",
            "Show the stored ladders: dates, rows, restated partitions and the\n"
            "last build. Reads local files only.",
            (Flag("--json", "machine-readable output"),),
        ),
        Command(
            "build",
            "Rebuild a ladder from local data (dry run unless --run)",
            "Recompute the FIFO lot ladder and compare it with the store. WITHOUT\n"
            "--run nothing is written: the plan lists new and restated dates. With\n"
            "--run the changed partitions are replaced atomically. No network.\n\n"
            "short_ladder (default): over FINRA short interest, EODHD prices and\n"
            "splits, one file per settlement date (seconds).\n"
            "long_ladder: over SEC 13F holdings (amendments resolved, the\n"
            "hedge-fund cohort from Form ADV, flows over managers present in both\n"
            "quarters), one file per quarter end (minutes).\n"
            "holdings_inputs: the cross-manager distribution of each 13F position\n"
            "(weight in holders' books, concentration, best ideas), same panel.",
            (
                Flag("--dataset", "short_ladder (default), long_ladder or holdings_inputs", metavar="<name>"),
                Flag("--run", "write the changed partitions (default is a dry run)"),
            ),
        ),
        Command(
            "qc",
            "Quality checks over a stored ladder",
            "Check the store against its state sidecar and its own invariants, and\n"
            "report whether the source is ahead of it. For the long ladder every\n"
            "stored level is reconciled with the sum of effective 13F holdings.",
            (Flag("--dataset", "short_ladder (default), long_ladder or holdings_inputs", metavar="<name>"),),
        ),
    )
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- derived point-in-time positioning datasets (ladders, holdings inputs)",
            "",
            f"Usage:  {PROG} <command> [flags]      (bare `{PROG}` == `{PROG} status`)",
            "",
            "Commands:",
            *ct.commands_block(COMMANDS),
            "",
            f"Run '{PROG} <command> --help' for the full help.",
        ]
    )


def _args(
    command: str, argv: list[str]
) -> tuple[dict[str, Any], list[str], int | None]:
    return ct.parse_or_exit(COMMANDS[command], argv, console=_render.make_console())


def _connect() -> Any:
    import explore_eodhd  # type: ignore[import-not-found]

    return explore_eodhd.connect()


def _dates(days: tuple[Any, ...]) -> str:
    if not days:
        return "-"
    if len(days) <= 4:
        return ", ".join(d.isoformat() for d in days)
    return f"{days[0].isoformat()} .. {days[-1].isoformat()} ({len(days)})"


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, _, done = _args("status", argv)
    if done is not None:
        return done
    root, source = pos_config.resolve_root()
    entries = [dataset.status(root, name) for name in dataset.DATASETS]
    if "--json" in flags:
        payload = {"root": str(root), "root_source": source, "datasets": entries}
        print(jsonlib.dumps(payload, indent=2))
        return 0
    console = _render.make_console()
    console.print(Text(f"root: {root}  ({source})", style="bold"))
    table = _render.minimal_table(title="datasets")
    for name in ("dataset", "dates", "first", "last", "rows", "restated", "last build"):
        table.add_column(name, no_wrap=True)
    dash = Text("-", style="dim")
    for entry in entries:
        table.add_row(
            entry["dataset"],
            str(entry["partitions"]) if entry["present"] else dash,
            entry["first"] or dash,
            entry["last"] or dash,
            _render.fmt_int(entry["rows"]) if entry["present"] else dash,
            str(entry["restated"]) if entry["present"] else dash,
            (entry["last_build"] or "")[:16].replace("T", " ") or dash,
        )
    console.print(table)
    for entry in entries:
        if not entry["present"]:
            console.print(
                Text(f"{entry['dataset']} not built yet -- run:  {dataset.spec_of(entry['dataset']).build_hint}", style="dim")
            )
    return 0


def _dataset_flag(flags: dict[str, Any], console: Any) -> dataset.Spec | None:
    name = str(flags.get("--dataset", dataset.DATASETS[0]))
    if name not in dataset.DATASETS:
        ct.bad_choice(console, "dataset", name, dataset.DATASETS)
        return None
    return dataset.spec_of(name)


def cmd_build(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("build", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"build takes flags only, got {rest}", style="red"))
        return 2
    spec = _dataset_flag(flags, console)
    if spec is None:
        return 2
    root, _ = pos_config.resolve_root()
    run = "--run" in flags
    report = dataset.build(_connect(), root, run=run, dataset=spec)
    verb = "built" if run else "plan (dry run)"
    console.print(Text(f"{spec.name} {verb}: {root}", style="bold"))
    if report.partitions == 0:
        console.print(
            Text(
                f"no {spec.source_hint} with prices found; fetch the source first",
                style="yellow",
            )
        )
        return 1
    console.print(
        f"  computed   {report.partitions} {spec.partition.replace('_', ' ')}s, "
        f"{_render.fmt_int(report.rows)} rows, {_render.fmt_int(report.symbols)} {spec.symbol}s"
    )
    if report.stats:
        console.print(
            "  left out   "
            + ", ".join(f"{k.replace('_', ' ')} {v}" for k, v in report.stats.items() if k not in ("securities", "periods"))
        )
    console.print(f"  new        {_dates(report.new)}")
    console.print(f"  restated   {_dates(report.restated)}")
    console.print(f"  unchanged  {report.unchanged}")
    if report.orphans:
        console.print(
            Text(
                f"  on disk but no longer computed: {_dates(report.orphans)}",
                style="yellow",
            )
        )
    if not run and report.changed:
        console.print(Text("nothing written -- add --run to store these", style="dim"))
    return 0


def cmd_qc(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("qc", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"qc takes flags only, got {rest}", style="red"))
        return 2
    spec = _dataset_flag(flags, console)
    if spec is None:
        return 2
    root, _ = pos_config.resolve_root()
    try:
        con = _connect()
    except Exception:
        con = None
    findings = dataset.qc(root, con, spec)
    if not findings:
        console.print(Text(f"{spec.name}: no findings", style="green"))
        return 0
    for finding in findings:
        style = "red" if finding.severity == "error" else "yellow"
        console.print(
            Text(f"{finding.severity:<5} {finding.check}: {finding.detail}", style=style)
        )
    return 1 if any(f.severity == "error" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"status": cmd_status, "build": cmd_build, "qc": cmd_qc}
    if command in ("-h", "--help", "help"):
        if rest and rest[0] in COMMANDS:
            print(command_help(rest[0]))
        else:
            print(top_help())
        return 0
    if command in dispatch:
        if command == "build":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("positioning", "build", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
