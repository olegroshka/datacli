"""``sec`` command group: status / fetch / qc for the SEC bulk datasets.

``fetch`` is a dry run unless ``--run``: it reads the SEC's listing page and
prints which Form 13F archives are due. The SEC requires a declared contact in
``SEC_USER_AGENT`` (environment or Windows user environment).
"""

from __future__ import annotations

import difflib
import json as jsonlib
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import sec  # noqa: E402,F401  (puts eodhd/ on sys.path)
import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

from sec import config as sec_config  # noqa: E402
from sec import adv, form13f  # noqa: E402

PROG = "sec"
DATASETS = ("form13f", "adv")

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What SEC data is on disk (bare `sec` does this)",
            "Show the stored Form 13F archives (count, period, filings, holdings rows)\n"
            "and the Form ADV adviser snapshots. Reads local files only.",
            (Flag("--json", "machine-readable output"),),
        ),
        Command(
            "fetch",
            "Fetch an SEC data set (dry run unless --run)",
            "form13f (default): read the SEC's listing page and fetch every Form 13F\n"
            "archive not stored yet, newest first, plus the newest archive again when\n"
            "the SEC has republished it; 20 to 100 MB each, several GB from 2013.\n"
            "adv: the monthly Form ADV adviser reports (one CSV per month since 2006,\n"
            "about 6 MB each). WITHOUT --run only the plan is printed.\n\n"
            "Needs SEC_USER_AGENT (a declared name and contact email).",
            (
                Flag("--dataset", "form13f (default) or adv", metavar="<name>"),
                Flag("--run", "download and store (default is a dry-run plan)"),
                Flag("--limit", "fetch at most N archives, newest first", metavar="<N>"),
                Flag("--full", "refetch archives already stored"),
            ),
        ),
        Command(
            "units",
            "Classify each stored filing's value units (dollars or thousands)",
            "Build the FILING_UNITS table for every stored archive that lacks it\n"
            "(archives fetched before the table existed), or for all with --full.\n"
            "Local only; `fetch` builds it for new archives itself.",
            (Flag("--full", "rebuild for every archive"),),
        ),
        Command(
            "qc",
            "Quality checks over the stored 13F archives",
            "Check that every stored archive has its tables and a state row, that no\n"
            "archive failed, and that filings are unique across archives; and that\n"
            "every ADV snapshot has a state row and a CRD column.",
        ),
    )
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- SEC bulk datasets (Form 13F institutional holdings)",
            "",
            f"Usage:  {PROG} <command> [flags]      (bare `{PROG}` == `{PROG} status`)",
            "",
            "Commands:",
            *ct.commands_block(COMMANDS),
            "",
            "fetch flags:",
            *ct.flags_block(COMMANDS["fetch"].flags),
            "",
            f"Run '{PROG} <command> --help' for the full help.",
        ]
    )


def _args(
    command: str, argv: list[str]
) -> tuple[dict[str, Any], list[str], int | None]:
    return ct.parse_or_exit(COMMANDS[command], argv, console=_render.make_console())


def collect_adv_status(root: Path) -> dict[str, Any]:
    store = adv.Store(root)
    state = store.load_state()
    ok = [s for s in state.values() if s.status == adv.STATUS_OK]
    return {
        "dataset": adv.NAME,
        "present": bool(ok),
        "archives": len(ok),
        "failed": sorted(s.snapshot for s in state.values() if s.status != adv.STATUS_OK),
        "first": min((s.date for s in ok), default=None),
        "last": max((s.date for s in ok), default=None),
        "filings": sum(s.advisers for s in ok),
        "holdings": sum(s.hedge_fund_advisers for s in ok),
        "bytes": sum(s.bytes for s in ok),
        "last_fetch": max((s.fetched_at for s in state.values()), default=None),
    }


def collect_status(root: Path) -> dict[str, Any]:
    store = form13f.Store(root)
    state = store.load_state()
    ok = [s for s in state.values() if s.status == form13f.STATUS_OK]
    return {
        "dataset": form13f.NAME,
        "present": bool(ok),
        "archives": len(ok),
        "failed": sorted(s.archive for s in state.values() if s.status != form13f.STATUS_OK),
        "first": min((s.start for s in ok), default=None),
        "last": max((s.end for s in ok), default=None),
        "filings": sum(s.filings for s in ok),
        "holdings": sum(s.holdings for s in ok),
        "bytes": sum(s.bytes for s in ok),
        "last_fetch": max((s.fetched_at for s in state.values()), default=None),
    }


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, _, done = _args("status", argv)
    if done is not None:
        return done
    root, source = sec_config.resolve_root()
    entries = [collect_status(root), collect_adv_status(root)]
    if "--json" in flags:
        payload = {"root": str(root), "root_source": source, "datasets": entries}
        print(jsonlib.dumps(payload, indent=2))
        return 0
    console = _render.make_console()
    console.print(Text(f"root: {root}  ({source})", style="bold"))
    table = _render.minimal_table(title="datasets")
    for name in ("dataset", "archives", "first", "last", "rows", "of which", "failed", "last fetch"):
        table.add_column(name, no_wrap=True)
    dash = Text("-", style="dim")
    for entry in entries:
        present = entry["present"]
        table.add_row(
            entry["dataset"],
            str(entry["archives"]) if present else dash,
            entry["first"] or dash,
            entry["last"] or dash,
            (_render.fmt_int(entry["filings"]) + (" filings" if entry["dataset"] == form13f.NAME else " advisers")) if present else dash,
            (_render.fmt_int(entry["holdings"]) + (" holdings" if entry["dataset"] == form13f.NAME else " hedge-fund advisers")) if present else dash,
            Text(str(len(entry["failed"])), style="red") if entry["failed"] else "0",
            (entry["last_fetch"] or "")[:16].replace("T", " ") or dash,
        )
    console.print(table)
    if not any(e["present"] for e in entries):
        console.print(Text("nothing fetched yet -- plan with:  sec fetch", style="dim"))
    return 0


def _session() -> Any:
    import requests

    return requests.Session()


def cmd_fetch(argv: list[str]) -> int:
    from rich.text import Text

    from finra.sec import user_agent

    flags, rest, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"fetch takes flags only, got {rest}", style="red"))
        return 2
    limit: int | None = None
    if "--limit" in flags:
        if not str(flags["--limit"]).isdigit() or int(flags["--limit"]) < 1:
            console.print(Text("--limit must be a positive integer", style="red"))
            return 2
        limit = int(flags["--limit"])
    dataset = str(flags.get("--dataset", DATASETS[0]))
    if dataset not in DATASETS:
        return ct.bad_choice(console, "dataset", dataset, DATASETS)
    agent = user_agent()
    if not agent:
        console.print(
            Text(
                "SEC_USER_AGENT is not set: the SEC requires a declared name and contact email",
                style="red",
            )
        )
        return 1
    root, _ = sec_config.resolve_root()
    run = "--run" in flags
    provider = form13f if dataset == form13f.NAME else adv
    try:
        report = provider.refresh(
            _session(),
            agent,
            root,
            run=run,
            full="--full" in flags,
            limit=limit,
            sleep=time.sleep,
            log=lambda message: console.print(f"  {message}"),
        )
    except (form13f.Form13FError, adv.AdvError) as exc:
        console.print(Text(str(exc), style="red"))
        return 1
    verb = "fetched" if run else "plan (dry run)"
    console.print(Text(f"{dataset} {verb}: {root}", style="bold"))
    console.print(f"  listed by the SEC  {report.listed} archives")
    planned = report.planned
    if not planned:
        console.print("  due                none; the store is current")
    else:
        console.print(f"  due                {len(planned)}: {planned[-1]} .. {planned[0]}")
    if run:
        rows = f", {_render.fmt_int(report.holdings)} holdings rows" if hasattr(report, "holdings") else ""
        console.print(f"  stored             {len(report.stored)} archives{rows}")
        for name, reason in report.failed:
            console.print(Text(f"  failed             {name}: {reason}", style="red"))
    elif planned:
        console.print(Text("nothing downloaded -- add --run to fetch these", style="dim"))
    return 0 if report.ok else 1


def cmd_units(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("units", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"units takes flags only, got {rest}", style="red"))
        return 2
    store = form13f.Store(sec_config.resolve_root()[0])
    todo = [
        a
        for a in store.archives_on_disk()
        if "--full" in flags or not store.table_path(form13f.UNITS_TABLE, a).exists()
    ]
    if not todo:
        console.print("every stored archive has its units table")
        return 0
    for archive in todo:
        count = form13f.build_units(store, archive)
        console.print(f"  {archive}: {count:,} filings classified")
    return 0


def qc(root: Path) -> list[tuple[str, str, str]]:
    """``(severity, check, detail)`` findings over the stored archives."""
    store = form13f.Store(root)
    state = store.load_state()
    on_disk = store.archives_on_disk()
    if not on_disk:
        return [("warn", "empty", "no 13F archives stored; run `sec fetch --run`")]
    findings: list[tuple[str, str, str]] = []
    failed = sorted(s.archive for s in state.values() if s.status != form13f.STATUS_OK)
    if failed:
        findings.append(("error", "failed_archives", f"{len(failed)}: {', '.join(failed[:5])}"))
    for archive in on_disk:
        missing = [
            t for t in form13f.REQUIRED_TABLES if not store.table_path(t, archive).exists()
        ]
        if missing:
            findings.append(("error", "table_missing", f"{archive}: {', '.join(missing)}"))
        if archive not in state:
            findings.append(("error", "state_missing", archive))
    no_units = [a for a in on_disk if not store.table_path(form13f.UNITS_TABLE, a).exists()]
    if no_units:
        findings.append(("warn", "units_missing", f"{len(no_units)} archives lack FILING_UNITS; run `sec units`"))
    stateless = sorted(a for a, s in state.items() if s.status == form13f.STATUS_OK and a not in on_disk)
    if stateless:
        findings.append(("error", "file_missing", f"{len(stateless)}: {', '.join(stateless[:5])}"))
    import duckdb

    con = duckdb.connect()
    glob = (store.table_dir("SUBMISSION") / "*.parquet").as_posix()
    dupes = con.execute(
        "SELECT count(*) FROM (SELECT ACCESSION_NUMBER FROM "
        f"read_parquet('{glob}', union_by_name=true) GROUP BY 1 HAVING count(*) > 1)"
    ).fetchone()[0]
    if dupes:
        findings.append(("warn", "filing_in_two_archives", f"{dupes} accession numbers appear more than once"))
    republished = [s.archive for s in state.values() if s.detail.startswith("republished")]
    if republished:
        findings.append(("warn", "republished", f"{len(republished)} archives changed after first fetch"))
    findings += qc_adv(root)
    return findings


def qc_adv(root: Path) -> list[tuple[str, str, str]]:
    store = adv.Store(root)
    state = store.load_state()
    on_disk = store.snapshots_on_disk()
    if not on_disk and not state:
        return []
    findings: list[tuple[str, str, str]] = []
    failed = sorted(s.snapshot for s in state.values() if s.status == adv.STATUS_ERROR)
    if failed:
        findings.append(("error", "adv_failed_snapshots", f"{len(failed)}: {', '.join(failed[:5])}"))
    absent = sorted(s.snapshot for s in state.values() if s.status == adv.STATUS_ABSENT)
    if absent:
        findings.append(("warn", "adv_absent_snapshots", f"{len(absent)} published as a notice, not data: {', '.join(absent[:5])}"))
    missing = [n for n in on_disk if n not in state]
    if missing:
        findings.append(("error", "adv_state_missing", f"{len(missing)} snapshots without a state row"))
    gone = sorted(n for n, s in state.items() if s.status == adv.STATUS_OK and n not in on_disk)
    if gone:
        findings.append(("error", "adv_file_missing", f"{len(gone)}: {', '.join(gone[:5])}"))
    return findings


def cmd_qc(argv: list[str]) -> int:
    from rich.text import Text

    _, rest, done = _args("qc", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"qc takes no arguments, got {rest}", style="red"))
        return 2
    findings = qc(sec_config.resolve_root()[0])
    if not findings:
        console.print(Text("form13f: no findings", style="green"))
        return 0
    for severity, check, detail in findings:
        style = "red" if severity == "error" else "yellow"
        console.print(Text(f"{severity:<5} {check}: {detail}", style=style))
    return 1 if any(f[0] == "error" for f in findings) else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"status": cmd_status, "fetch": cmd_fetch, "units": cmd_units, "qc": cmd_qc}
    if command in ("-h", "--help", "help"):
        if rest and rest[0] in COMMANDS:
            print(command_help(rest[0]))
        else:
            print(top_help())
        return 0
    if command in dispatch:
        if command == "fetch":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("sec", "fetch", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
