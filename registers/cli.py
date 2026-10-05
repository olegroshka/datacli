"""``registers`` command group: status / fetch / qc for the public short registers.

``fetch`` downloads each market's published history file (the FCA workbook,
the AMF CSV through the data.gouv.fr API), parses it strictly, and with
``--run`` replaces the stored history when the file changed; without
``--run`` it only reports what it would store.
"""

from __future__ import annotations

import difflib
import json as jsonlib
import sys
from dataclasses import replace
from typing import Any

import registers  # noqa: F401  (puts eodhd/ on sys.path)
import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

from registers import amf, common, eu, fca, holdings  # noqa: E402
from registers import config as registers_config  # noqa: E402

PROG = "registers"

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What register histories are on disk (bare `registers` does this)",
            "Show the stored histories per market: file date, rows, last fetch.",
            (Flag("--json", "machine-readable output"), Flag("--holdings", "the long-side register stores (nl, de) instead")),
        ),
        Command(
            "fetch",
            "Fetch the published histories (dry run unless --run)",
            "Download each market's published file(s) (uk: the FCA workbook, frozen since\n"
            "2026-07-11; fr: the AMF CSV named by the data.gouv.fr API; nl, se, no, ie, de:\n"
            "the AFM, Finansinspektionen, Finanstilsynet, Central Bank of Ireland and\n"
            "Bundesanzeiger files), parse them strictly,\n"
            "and replace the stored history when the file changed. A file with fewer rows\n"
            "than the stored one is refused. WITHOUT --run nothing is written.",
            (
                Flag("--run", "store the histories (default is a dry run)"),
                Flag("--market", "one market only: uk, fr, nl, se, no, ie or de (default: all)", metavar="MARKET"),
                Flag("--holdings", "the long-side registers instead (nl: the AFM's holdings and capital exports; de: BaFin's snapshot), accumulated, never shrinking"),
                Flag("--from-dir", "with --holdings: read saved exports from DIR instead of fetching (seeding; the file's date is its modification date)", metavar="DIR"),
            ),
        ),
        Command(
            "qc",
            "Quality checks over the stored histories",
            "Check that the state and the files agree, that percentages and dates are\n"
            "sane, and report duplicates and staleness.",
        ),
    )
}

FETCHERS = {
    "uk": (fca.fetch, lambda data, source: fca.parse(data)),
    "fr": (amf.fetch, lambda data, source: amf.parse(data, file_date=amf.file_date_of(source))),
    **{market: (fetch, (lambda parse: (lambda data, source: parse(data)))(parse)) for market, (fetch, parse) in eu.MARKETS.items()},
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- public net short position registers, per holder (FCA, AMF, AFM, FI, Finanstilsynet, CBI, Bundesanzeiger)",
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


def refresh_market(market: str, root, *, run: bool) -> common.FetchReport:
    fetch, parse = FETCHERS[market]
    holder: dict[str, str] = {}

    def _fetch() -> tuple[bytes, str]:
        data, source = fetch()
        holder["source"] = source
        return data, source

    return common.refresh(market, _fetch, lambda data: parse(data, holder.get("source", "")), root, run=run,
                          accumulate=market in common.WINDOWED_MARKETS)


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, _, done = _args("status", argv)
    if done is not None:
        return done
    root, source = registers_config.resolve_root()
    long_side = "--holdings" in flags
    entries = holdings.status_holdings(root) if long_side else common.status(root)
    if "--json" in flags:
        print(jsonlib.dumps({"root": str(root), "root_source": source, "kind": "holdings" if long_side else "short", "markets": entries}, indent=2))
        return 0
    console = _render.make_console()
    console.print(Text(f"root: {root}  ({source})", style="bold"))
    table = _render.minimal_table(title="long-side registers (holdings)" if long_side else "markets")
    for name in ("market", "file date", "rows", "bytes", "last fetch", "previous kept"):
        if long_side and name == "previous kept":
            continue
        table.add_column(name, no_wrap=True)
    dash = Text("-", style="dim")
    for e in entries:
        present = e["present"]
        cells = [
            e["market"],
            e["file_date"] or dash,
            _render.fmt_int(e["rows"]) if present else dash,
            _render.fmt_int(e["bytes"]) if present else dash,
            (e["last_fetch"] or "")[:16].replace("T", " ") or dash,
        ]
        if not long_side:
            cells.append(("yes" if e["previous"] else "no") if present else dash)
        table.add_row(*cells)
    console.print(table)
    if not any(e["present"] for e in entries):
        console.print(Text("nothing stored yet -- run:  registers fetch " + ("--holdings " if long_side else "") + "--run", style="dim"))
    return 0


def refresh_holdings_market(market: str, root, *, run: bool, from_dir: str | None) -> holdings.HoldingsReport:
    from pathlib import Path

    fetch = (lambda: holdings.read_dir(market, Path(from_dir))) if from_dir else holdings.FETCHERS[market]
    return holdings.refresh_holdings(market, fetch, root, run=run)


def cmd_fetch(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"fetch takes flags only, got {rest}", style="red"))
        return 2
    market = flags.get("--market")
    long_side = "--holdings" in flags
    from_dir = flags.get("--from-dir")
    if from_dir is not None and (not long_side or from_dir is True):
        console.print(Text("--from-dir DIR needs --holdings and a directory", style="red"))
        return 2
    universe = holdings.HOLDINGS_MARKETS if long_side else common.MARKETS
    markets = list(universe) if market in (None, True) else [str(market)]
    for m in markets:
        if m not in universe:
            console.print(Text(f"unknown market {m!r}; expected one of {', '.join(universe)}", style="red"))
            return 2
    root, _ = registers_config.resolve_root()
    run = "--run" in flags
    verb = "fetched" if run else "plan (dry run)"
    console.print(Text(f"registers {'holdings ' if long_side else ''}{verb}: {root}", style="bold"))
    failed = False
    for m in markets:
        report = refresh_holdings_market(m, root, run=run, from_dir=str(from_dir) if from_dir else None) if long_side else refresh_market(m, root, run=run)
        if report.outcome == "failed":
            console.print(Text(f"  {m:<3} failed           {report.detail}", style="red"))
            failed = True
            continue
        assert report.file_date is not None
        line = f"  {m:<3} file dated {report.file_date.isoformat()}, {_render.fmt_int(report.rows)} rows: "
        if report.outcome == "unchanged":
            console.print(line + "already stored with this content")
        elif report.outcome == "planned":
            console.print(Text(line + "nothing written -- add --run to store it", style="dim"))
        else:
            window = report.detail.split("; ", 1)[1] if "; window" in report.detail else ""
            console.print(line + report.outcome + (f" ({window})" if window else ""))
    if failed:
        return 1
    if all(True for _ in markets) and not run:
        return 0
    print("Everything in sync.")
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
    findings = common.qc(registers_config.resolve_root()[0])
    if not findings:
        console.print(Text("registers: no findings", style="green"))
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

            with direct_mutation_lock("registers", "fetch", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
