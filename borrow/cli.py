"""``borrow`` command group: status / fetch / qc for the broker's shortable list.

``fetch`` downloads today's ``usa.txt`` and its md5 from Interactive Brokers'
anonymous FTP (no credentials), verifies and parses it strictly, and with
``--run`` stores it as the day's snapshot; without ``--run`` it only reports
what it would store.
"""

from __future__ import annotations

import difflib
import json as jsonlib
import sys
from dataclasses import replace
from typing import Any

import borrow  # noqa: F401  (puts eodhd/ on sys.path)
import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

from borrow import config as borrow_config  # noqa: E402
from borrow import ib  # noqa: E402

PROG = "borrow"

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What borrow snapshots are on disk (bare `borrow` does this)",
            "Show the stored snapshots of Interactive Brokers' shortable list:\n"
            "count, first and last day, rows, replaced days, last fetch.",
            (Flag("--json", "machine-readable output"),),
        ),
        Command(
            "fetch",
            "Fetch today's shortable list (dry run unless --run)",
            "Download usa.txt and its md5 companion from ftp2.interactivebrokers.com\n"
            "(anonymous, no credentials), verify the digest, parse strictly, and store\n"
            "the file as the snapshot of its own date. A day already stored with the\n"
            "same content is left alone; a changed file replaces the day's snapshot.\n"
            "WITHOUT --run nothing is written.",
            (Flag("--run", "store the snapshot (default is a dry run)"),),
        ),
        Command(
            "qc",
            "Quality checks over the stored snapshots",
            "Check that files and state rows agree, that keys are unique, and\n"
            "report gaps and staleness.",
        ),
    )
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- a broker's shortable list, one snapshot a day (Interactive Brokers)",
            "",
            f"Usage:  {PROG} <command> [flags]      (bare `{PROG}` == `{PROG} status`)",
            "",
            "Commands:",
            *ct.commands_block(COMMANDS),
            "",
            f"Run '{PROG} <command> --help' for the full help.",
        ]
    )


def _args(command: str, argv: list[str]) -> tuple[dict[str, Any], list[str], int | None]:
    return ct.parse_or_exit(COMMANDS[command], argv, console=_render.make_console())


def _ftp() -> Any:
    return ib.ftp_connect()


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, _, done = _args("status", argv)
    if done is not None:
        return done
    root, source = borrow_config.resolve_root()
    entry = ib.status(root)
    if "--json" in flags:
        print(jsonlib.dumps({"root": str(root), "root_source": source, "datasets": [entry]}, indent=2))
        return 0
    console = _render.make_console()
    console.print(Text(f"root: {root}  ({source})", style="bold"))
    table = _render.minimal_table(title="datasets")
    for name in ("dataset", "snapshots", "first", "last", "rows", "replaced", "last fetch"):
        table.add_column(name, no_wrap=True)
    dash = Text("-", style="dim")
    present = entry["present"]
    table.add_row(
        entry["dataset"],
        str(entry["snapshots"]) if present else dash,
        entry["first"] or dash,
        entry["last"] or dash,
        _render.fmt_int(entry["rows"]) if present else dash,
        str(entry["replaced"]) if present else dash,
        (entry["last_fetch"] or "")[:16].replace("T", " ") or dash,
    )
    console.print(table)
    if not present:
        console.print(Text("nothing stored yet -- run:  borrow fetch --run", style="dim"))
    return 0


def cmd_fetch(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"fetch takes flags only, got {rest}", style="red"))
        return 2
    root, _ = borrow_config.resolve_root()
    run = "--run" in flags
    report = ib.refresh(_ftp, root, run=run)
    verb = "fetched" if run else "plan (dry run)"
    console.print(Text(f"{ib.NAME} {verb}: {root}", style="bold"))
    if report.outcome == "failed":
        console.print(Text(f"  failed             {report.detail}", style="red"))
        return 1
    assert report.stamp is not None
    console.print(f"  file stamp         {report.stamp.isoformat(sep=' ')} (New York), {_render.fmt_int(report.rows)} rows")
    if report.outcome == "unchanged":
        console.print("  snapshot           already stored with this content")
        print("Everything in sync.")
    elif report.outcome == "planned":
        console.print(Text("  nothing written -- add --run to store this snapshot", style="dim"))
    else:
        console.print(f"  snapshot           {report.outcome} for {report.stamp.date().isoformat()}")
    return 0


def cmd_qc(argv: list[str]) -> int:
    from rich.text import Text

    _, rest, done = _args("qc", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"qc takes no arguments, got {rest}", style="red"))
        return 2
    findings = ib.qc(borrow_config.resolve_root()[0])
    if not findings:
        console.print(Text(f"{ib.NAME}: no findings", style="green"))
        return 0
    for severity, check, detail in findings:
        console.print(Text(f"{severity:<5} {check}: {detail}", style="red" if severity == "error" else "yellow"))
    return 1 if any(f[0] == "error" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"status": cmd_status, "fetch": cmd_fetch, "qc": cmd_qc}
    if command in ("-h", "--help", "help"):
        if rest and rest[0] in COMMANDS:
            print(command_help(rest[0]))
        else:
            print(top_help())
        return 0
    if command in dispatch:
        if command == "fetch":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("borrow", "fetch", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
