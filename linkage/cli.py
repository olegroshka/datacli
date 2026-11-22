"""``linkage`` command group: status / build / qc for the derived link datasets.

``build`` recomputes every partition from local data (no network) and reports
which are new, restated or unchanged; ``--run`` writes the changed ones. The
datasets build in dependency order: etf_betas, auction_abnormal,
auction_cospike (which reads auction_abnormal).
"""

from __future__ import annotations

import datetime as dt
import difflib
import json as jsonlib
import sys
from dataclasses import replace
from typing import Any

import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

import linkage  # noqa: F401  (puts eodhd/ and the repo root on sys.path)
from linkage import config, qc  # noqa: E402
from linkage.config import Params, digest  # noqa: E402
from linkage.datasets import (  # noqa: E402
    AUCTION_ABNORMAL,
    AUCTION_COSPIKE,
    ETF_BETAS,
    NAMES,
    SCHEMAS,
    AuctionAbnormal,
    AuctionCospike,
    Dataset,
    DatasetPlan,
    EtfBetas,
    apply,
)
from linkage.sources import AlpacaAuctions, IntradayFiles  # noqa: E402
from linkage.store import NEW, RESTATED, UNCHANGED, PartitionStore  # noqa: E402

PROG = "linkage"
DATASET_FLAG = Flag("--dataset", f"one dataset: {' | '.join(NAMES)}", metavar="NAME")

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What linkage datasets are built (bare `linkage` does this)",
            "Show each derived dataset: partitions, first and last, rows, whether it\n"
            "was built with the current parameters, and when.",
            (Flag("--json", "machine-readable output"),),
        ),
        Command(
            "build",
            "Rebuild the link datasets from local data (dry run unless --run)",
            "Recompute every partition from the eodhd daily lanes, the intraday\n"
            "membership and the Alpaca auctions (no network), and report which are\n"
            "new, restated (inputs or parameters moved) or unchanged. With --run the\n"
            "changed partitions are written atomically. --full rewrites all of them.\n"
            "WITHOUT --run nothing is written.",
            (
                Flag("--run", "write the changed partitions (default is a dry run)"),
                Flag("--full", "rewrite every partition, changed or not"),
                DATASET_FLAG,
            ),
        ),
        Command(
            "qc",
            "Quality checks over the built datasets",
            "Check that files and manifests agree, that keys are unique and values in\n"
            "range, and that the market betas and the daily centring look right.",
            (DATASET_FLAG,),
        ),
    )
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- point-in-time links between names: ETF betas, auction co-spikes (derived, local)",
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


def _stores() -> dict[str, PartitionStore]:
    root = config.linkage_root()
    return {name: PartitionStore(root, name, SCHEMAS[name]) for name in NAMES}


def _selected(flags: dict[str, Any], console: Any) -> list[str] | None:
    name = flags.get("--dataset")
    if name is None:
        return list(NAMES)
    if name not in NAMES:
        ct.bad_choice(console, "dataset", str(name), NAMES)
        return None
    return [str(name)]


def _today() -> dt.date:
    return dt.date.today()


def build_datasets(
    names: list[str], params: Params, stores: dict[str, PartitionStore]
) -> list[Dataset]:
    """The composition root: wire each dataset to its adapters."""
    from daily_store import DailyStore  # type: ignore[import-not-found]

    eodhd_root = config.eodhd_root()
    universe = IntradayFiles(eodhd_root)
    abnormal = AuctionAbnormal(
        AlpacaAuctions(config.alpaca_root()),
        universe,
        stores[AUCTION_ABNORMAL],
        params.auction_abnormal,
    )

    def panel() -> Any:
        # built together: hand over the fresh panel; built alone: read what is stored
        return (
            abnormal.panel
            if abnormal.panel is not None
            else stores[AUCTION_ABNORMAL].read_all()
        )

    every: dict[str, Dataset] = {
        ETF_BETAS: EtfBetas(
            DailyStore(eodhd_root),
            universe,
            stores[ETF_BETAS],
            params.etf_betas,
            today=_today(),
        ),
        AUCTION_ABNORMAL: abnormal,
        AUCTION_COSPIKE: AuctionCospike(
            panel, universe, stores[AUCTION_COSPIKE], params.auction_cospike
        ),
    }
    return [every[name] for name in names]


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status_payload() -> dict[str, Any]:
    root, source = config.resolve_root()
    try:
        params = config.load_params()
    except ValueError:
        params = None
    current = {
        ETF_BETAS: digest(params.etf_betas) if params else None,
        AUCTION_ABNORMAL: digest(params.auction_abnormal) if params else None,
        AUCTION_COSPIKE: digest(params.auction_cospike) if params else None,
    }
    datasets = []
    for name, store in _stores().items():
        keys = store.keys_on_disk()
        manifest = store.manifest()
        entries = store.entries()
        datasets.append(
            {
                "dataset": name,
                "partitions": len(keys),
                "first": keys[0] if keys else None,
                "last": keys[-1] if keys else None,
                "rows": sum(e.rows for e in entries.values()),
                "current_params": bool(keys) and manifest["params"] == current[name],
                "last_built": max((e.built_at for e in entries.values()), default=None),
            }
        )
    return {"root": str(root), "root_source": source, "datasets": datasets}


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("status", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"status takes flags only, got {rest}", style="red"))
        return 2
    payload = status_payload()
    if "--json" in flags:
        print(jsonlib.dumps(payload, indent=2))
        return 0
    console.print(
        Text(f"root: {payload['root']}  ({payload['root_source']})", style="bold")
    )
    table = _render.minimal_table(title="datasets")
    for name in (
        "dataset",
        "partitions",
        "first",
        "last",
        "rows",
        "params",
        "last built",
    ):
        table.add_column(name, no_wrap=True)
    for d in payload["datasets"]:
        built = d["partitions"] > 0
        table.add_row(
            d["dataset"],
            str(d["partitions"]) if built else "-",
            d["first"] or "-",
            d["last"] or "-",
            _render.fmt_int(d["rows"]) if built else "-",
            ("current" if d["current_params"] else "changed") if built else "-",
            (d["last_built"] or "")[:16].replace("T", " ") or "-",
        )
    console.print(table)
    if not any(d["partitions"] for d in payload["datasets"]):
        console.print(
            Text(
                "nothing built yet -- run:  linkage build  (plan), then  linkage build --run",
                style="dim",
            )
        )
    return 0


# --------------------------------------------------------------------------- #
# build
# --------------------------------------------------------------------------- #
def cmd_build(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("build", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"build takes flags only, got {rest}", style="red"))
        return 2
    names = _selected(flags, console)
    if names is None:
        return 2
    try:
        params = config.load_params()
    except ValueError as exc:
        console.print(Text(str(exc), style="red"))
        return 2
    run, full = "--run" in flags, "--full" in flags
    stores = _stores()
    failed = False
    for dataset in build_datasets(names, params, stores):
        try:
            plan = apply(dataset.plan(), stores[dataset.name], run=run, full=full)
        except (FileNotFoundError, ValueError) as exc:
            console.print(Text(f"{dataset.name}: not built -- {exc}", style="red"))
            failed = True
            continue
        _print_plan(console, plan, run)
    return 1 if failed else 0


def _print_plan(console: Any, plan: DatasetPlan, run: bool) -> None:
    from rich.text import Text

    new, restated, unchanged = (
        plan.count(NEW),
        plan.count(RESTATED),
        plan.count(UNCHANGED),
    )
    span = (
        f"{plan.partitions[0].key}..{plan.partitions[-1].key}"
        if plan.partitions
        else "none"
    )
    console.print(
        Text(f"{plan.name}: {len(plan.partitions)} partitions ({span})", style="bold")
    )
    for note in plan.notes:
        console.print(Text(f"  {note}", style="dim"))
    changed = new + restated + len(plan.stale)
    verb = "written" if run else "would write"
    console.print(
        f"  {new} new, {restated} restated, {unchanged} unchanged, {len(plan.stale)} dropped -- {verb} {changed}"
    )
    if changed and not run:
        console.print(
            Text("  nothing written -- add --run to store these", style="dim")
        )


# --------------------------------------------------------------------------- #
# qc
# --------------------------------------------------------------------------- #
def cmd_qc(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("qc", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"qc takes flags only, got {rest}", style="red"))
        return 2
    names = _selected(flags, console)
    if names is None:
        return 2
    stores = _stores()
    found = [f for name in names for f in qc.findings(stores[name])]
    if not found:
        console.print(Text(f"{', '.join(names)}: no findings", style="green"))
        return 0
    for severity, check, detail in found:
        console.print(
            Text(
                f"{severity:<5} {check}: {detail}",
                style="red" if severity == "error" else "yellow",
            )
        )
    return 1 if any(f[0] == "error" for f in found) else 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"status": cmd_status, "build": cmd_build, "qc": cmd_qc}
    if command in ("-h", "--help", "help"):
        print(command_help(rest[0]) if rest and rest[0] in COMMANDS else top_help())
        return 0
    if command in dispatch:
        if command == "build":
            from scheduler.commands import direct_mutation_lock
            from scheduler.locks import LockUnavailable

            try:
                with direct_mutation_lock("linkage", "build", rest):
                    return dispatch[command](rest)
            except LockUnavailable as exc:
                print(f"not started: {_render.describe_lock(exc)}", file=sys.stderr)
                return 1
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
