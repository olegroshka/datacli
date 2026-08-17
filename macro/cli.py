"""``macro`` command group: list / status / fetch the FRED + EODHD macro data.

Dry-run by default; ``fetch --run`` performs the actual pull. ``--provider
fred|eodhd|all`` selects the source. Rendered in the shared ``_render`` palette.
"""

from __future__ import annotations

import difflib
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from macro import config as macro_config  # noqa: E402
from macro import eodhd, fred  # noqa: E402
from macro import registry as reg  # noqa: E402

_EODHD = _REPO / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
import _render  # type: ignore[import-not-found]  # noqa: E402

PROG = "macro"
PROVIDERS = ("fred", "eodhd", "all")


# --------------------------------------------------------------------------- #
# command table -- the ONE source of truth for usage strings and flags
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Flag:
    """A ``--flag`` a macro command accepts (``metavar`` set => takes a value)."""

    name: str
    help: str
    metavar: str = ""


@dataclass(frozen=True)
class Command:
    """One macro command: usage, one-liner, longer help, accepted flags."""

    name: str
    summary: str
    detail: str = ""
    flags: tuple[Flag, ...] = ()

    @property
    def synopsis(self) -> str:
        """``fetch [--provider <p>] [--run] [--full]`` -- no program prefix."""
        parts = [self.name] + [
            f"[{f.name} {f.metavar}]" if f.metavar else f"[{f.name}]"
            for f in self.flags
        ]
        return " ".join(parts)

    @property
    def usage(self) -> str:
        return f"{PROG} {self.synopsis}"


_KEYS_NOTE = (
    "Keys: FRED needs FRED_API_KEY; the EODHD side needs EODHD_API_KEY (env, the\n"
    "Windows user environment, or the eodhd key file). Both are read from the\n"
    "environment, never stored."
)

COMMANDS: dict[str, Command] = {
    c.name: c
    for c in (
        Command(
            "list",
            "FRED series + EODHD indicators / market symbols",
            "List everything the registry knows how to fetch: FRED series, EODHD\n"
            "country indicators (x countries) and EODHD end-of-day market symbols.",
        ),
        Command(
            "status",
            "What macro data is on disk (bare `macro` does this)",
            "Show what is on disk under the macro root, per provider: FRED series\n"
            "(rows, first/last date), EODHD indicators (countries, rows) and EODHD\n"
            "market symbols. Reads local parquet only; needs no key.",
        ),
        Command(
            "fetch",
            "Fetch to parquet (dry-run plan unless --run)",
            "Fetch macro data to parquet under the macro root. WITHOUT --run this is\n"
            "a dry run: it prints the plan and whether each provider's key is set,\n"
            "and touches nothing. With --run it pulls, incrementally merging into the\n"
            f"existing parquet unless --full.\n\n{_KEYS_NOTE}",
            (
                Flag(
                    "--provider",
                    f"which source to fetch: {' | '.join(PROVIDERS)} (default: all)",
                    metavar="<name>",
                ),
                Flag("--run", "actually fetch (default is a dry-run plan)"),
                Flag("--full", "overwrite instead of the default incremental merge"),
            ),
        ),
    )
}


class _HelpRequested(Exception):
    """``-h`` / ``--help`` seen: print the command's help and stop."""


class _UsageError(Exception):
    """A flag we do not know or that is malformed; message is user-facing."""


def _parse(command: str, argv: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Split ``argv`` into ``(flags, positionals)`` for ``command``.

    Boolean flags map to ``True``; value flags accept ``--name=value`` and
    ``--name value``. ``-h`` / ``--help`` win before anything else.

    Raises:
        _HelpRequested: on ``-h`` / ``--help``.
        _UsageError: on an unknown flag (with a did-you-mean hint), a value flag
            without a value, or a boolean flag given a value.
    """
    spec = {f.name: f for f in COMMANDS[command].flags}
    if any(tok in ("-h", "--help") for tok in argv):
        raise _HelpRequested()
    flags: dict[str, Any] = {}
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        i += 1
        if not tok.startswith("--"):
            rest.append(tok)
            continue
        name, eq, value = tok.partition("=")
        flag = spec.get(name)
        if flag is None:
            near = difflib.get_close_matches(name, list(spec), n=1, cutoff=0.5)
            hint = f" -- did you mean {near[0]}?" if near else ""
            raise _UsageError(f"unknown flag {name}{hint}")
        if flag.metavar:
            if not eq:
                if i >= len(argv) or argv[i].startswith("--"):
                    raise _UsageError(f"flag {name} needs a value ({flag.metavar})")
                value = argv[i]
                i += 1
            flags[name] = value
        else:
            if eq:
                raise _UsageError(f"flag {name} does not take a value")
            flags[name] = True
    return flags, rest


def command_help(name: str) -> str:
    """The per-command help block (usage, description, flags)."""
    cmd = COMMANDS[name]
    rows = [(f"{f.name} {f.metavar}".rstrip(), f.help) for f in cmd.flags]
    rows.append(("-h, --help", "show this help"))
    width = max(len(label) for label, _ in rows)
    lines = [f"usage: {cmd.usage}", "", cmd.detail or cmd.summary, "", "flags:"]
    lines += [f"  {label:<{width}}  {text}" for label, text in rows]
    return "\n".join(lines)


def _args(
    command: str, argv: list[str]
) -> tuple[dict[str, Any], list[str], int | None]:
    """Parse ``argv`` for ``command``; the third item is an exit code when done.

    ``--help`` prints the command help and yields ``0``; a bad flag prints a
    friendly error plus the usage line and yields ``2``. Otherwise ``None`` and
    the caller proceeds with ``(flags, positionals)`` -- only then may any
    network or disk work start.
    """
    try:
        flags, rest = _parse(command, argv)
    except _HelpRequested:
        print(command_help(command))
        return {}, [], 0
    except _UsageError as exc:
        from rich.text import Text

        cmd = COMMANDS[command]
        console = _render.make_console()
        console.print(Text(str(exc), style="red"))
        console.print(
            Text(f"usage: {cmd.usage}   ({PROG} {cmd.name} --help)", style="dim")
        )
        return {}, [], 2
    return flags, rest, None


def cmd_list(argv: list[str]) -> int:
    """List the registered FRED series and EODHD indicators."""
    from rich.text import Text

    _, _, done = _args("list", argv)
    if done is not None:
        return done
    console = _render.make_console()

    fred_table = _render.boxed_table(title=f"FRED series ({len(reg.FRED_SERIES)})")
    fred_table.add_column("series_id", style="cyan", no_wrap=True)
    fred_table.add_column("name")
    fred_table.add_column("category", no_wrap=True)
    for s in reg.FRED_SERIES.values():
        fred_table.add_row(s.series_id, s.name, s.category)
    console.print(fred_table)

    ind_table = _render.boxed_table(
        title=f"EODHD indicators ({len(reg.EODHD_INDICATORS)}) "
        f"× {len(reg.EODHD_COUNTRIES)} countries"
    )
    ind_table.add_column("indicator", style="cyan", no_wrap=True)
    ind_table.add_column("name")
    ind_table.add_column("category", no_wrap=True)
    for i in reg.EODHD_INDICATORS.values():
        ind_table.add_row(i.indicator, i.name, i.category)
    console.print(ind_table)
    console.print(Text("countries: " + ", ".join(reg.EODHD_COUNTRIES), style="dim"))

    mkt_table = _render.boxed_table(
        title=f"EODHD market series ({len(reg.EODHD_MARKET)})"
    )
    mkt_table.add_column("symbol", style="cyan", no_wrap=True)
    mkt_table.add_column("name")
    mkt_table.add_column("category", no_wrap=True)
    for m in reg.EODHD_MARKET.values():
        mkt_table.add_row(m.symbol, m.name, m.category)
    console.print(mkt_table)
    return 0


def _fred_status(console: object, root: Path) -> None:
    from rich.text import Text

    frame = fred.load(root)
    if frame is None or frame.empty:
        console.print(  # type: ignore[attr-defined]
            "[yellow]no FRED data[/yellow] — fetch with:  macro fetch --provider fred --run"
        )
        return
    stats = (
        frame.groupby("series_id")["date"].agg(["size", "min", "max"]).to_dict("index")
    )
    table = _render.minimal_table(
        title=f"FRED · {frame['series_id'].nunique()} series · {len(frame):,} rows"
    )
    for col in ("series_id", "name"):
        table.add_column(col, no_wrap=True)
    table.add_column("rows", justify="right", no_wrap=True)
    table.add_column("first", no_wrap=True)
    table.add_column("last", no_wrap=True)
    for sid, s in reg.FRED_SERIES.items():
        row = stats.get(sid)
        if row is not None:
            table.add_row(
                sid,
                s.name,
                f"{int(row['size']):,}",
                str(row["min"])[:10],
                str(row["max"])[:10],
            )
        else:
            table.add_row(
                sid, s.name, Text("-", style="dim"), Text("missing", style="yellow"), ""
            )
    console.print(table)  # type: ignore[attr-defined]


def _eodhd_status(console: object, root: Path) -> None:
    from rich.text import Text

    frame = eodhd.load(root)
    if frame is None or frame.empty:
        console.print(  # type: ignore[attr-defined]
            "[yellow]no EODHD macro data[/yellow] — fetch with:  macro fetch --provider eodhd --run"
        )
        return
    by_ind = frame.groupby("indicator").agg(
        countries=("country", "nunique"), rows=("value", "size")
    )
    stats = by_ind.to_dict("index")
    table = _render.minimal_table(
        title=f"EODHD · {frame['country'].nunique()} countries · {len(frame):,} rows"
    )
    table.add_column("indicator", style="cyan", no_wrap=True)
    table.add_column("countries", justify="right", no_wrap=True)
    table.add_column("rows", justify="right", no_wrap=True)
    for ind in reg.EODHD_INDICATORS:
        row = stats.get(ind)
        if row is not None:
            table.add_row(ind, str(int(row["countries"])), f"{int(row['rows']):,}")
        else:
            table.add_row(ind, Text("-", style="dim"), Text("missing", style="yellow"))
    console.print(table)  # type: ignore[attr-defined]


def _market_status(console: object, root: Path) -> None:
    from rich.text import Text

    frame = eodhd.load_market(root)
    if frame is None or frame.empty:
        console.print(  # type: ignore[attr-defined]
            "[yellow]no EODHD market data[/yellow] — fetch with:  macro fetch --provider eodhd --run"
        )
        return
    stats = frame.groupby("symbol")["date"].agg(["size", "min", "max"]).to_dict("index")
    table = _render.minimal_table(
        title=f"EODHD market · {frame['symbol'].nunique()} symbols · {len(frame):,} rows"
    )
    for col in ("symbol", "name"):
        table.add_column(col, no_wrap=True)
    table.add_column("rows", justify="right", no_wrap=True)
    table.add_column("first", no_wrap=True)
    table.add_column("last", no_wrap=True)
    for sym, m in reg.EODHD_MARKET.items():
        row = stats.get(sym)
        if row is not None:
            table.add_row(
                sym,
                m.name,
                f"{int(row['size']):,}",
                str(row["min"])[:10],
                str(row["max"])[:10],
            )
        else:
            table.add_row(
                sym, m.name, Text("-", style="dim"), Text("missing", style="yellow"), ""
            )
    console.print(table)  # type: ignore[attr-defined]


def cmd_status(argv: list[str]) -> int:
    """Show what macro data is on disk, per provider."""
    _, _, done = _args("status", argv)
    if done is not None:
        return done
    console = _render.make_console()
    root = macro_config.macro_root()
    _fred_status(console, root)
    _eodhd_status(console, root)
    _market_status(console, root)
    return 0


def cmd_fetch(argv: list[str]) -> int:
    """Fetch macro data to parquet (dry-run unless --run). --provider fred|eodhd|all."""
    from rich.text import Text

    flags, _, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    run = "--run" in flags
    full = "--full" in flags  # overwrite instead of the default incremental merge
    provider = flags.get("--provider", "all")
    if provider not in PROVIDERS:
        near = difflib.get_close_matches(provider, PROVIDERS, n=1)
        hint = f" -- did you mean {near[0]}?" if near else ""
        console.print(f"[red]unknown provider '{provider}'[/red]{hint}")
        console.print(Text(f"choices: {'|'.join(PROVIDERS)}", style="dim"))
        return 2
    root = macro_config.macro_root()
    do_fred = provider in ("fred", "all")
    do_eodhd = provider in ("eodhd", "all")

    if not run:
        console.print(Text(f"plan -> {root}", style="bold"))
        if do_fred:
            key = bool(fred.api_key())
            console.print(
                Text(
                    f"  FRED   {len(reg.FRED_SERIES)} series   "
                    f"key: {'set' if key else 'NOT SET'}",
                    style="" if key else "red",
                )
            )
        if do_eodhd:
            key = bool(eodhd.api_key())
            console.print(
                Text(
                    f"  EODHD  {len(reg.eodhd_pairs())} indicator pairs + "
                    f"{len(reg.EODHD_MARKET)} market symbols   "
                    f"key: {'set' if key else 'NOT SET'}",
                    style="" if key else "red",
                )
            )
        console.print(Text("re-run with --run to fetch", style="dim"))
        return 0

    rc = 0
    if do_fred:
        try:
            r = fred.refresh(reg.fred_ids(), run=True, root=root, full_refresh=full)
            msg = f"FRED: {r['series_with_data']}/{r['series']} series · {r['rows']:,} rows"
            if r["failed"]:
                msg += f" · failed: {', '.join(r['failed'])}"
            console.print(Text(msg, style="green" if not r["failed"] else "yellow"))
        except Exception as exc:
            console.print(f"[red]FRED {type(exc).__name__}[/red]: {exc}")
            rc = 1
    if do_eodhd:
        try:
            r = eodhd.refresh(reg.eodhd_pairs(), run=True, root=root, full_refresh=full)
            console.print(
                Text(
                    f"EODHD indicators: {r['series_with_data']}/{r['pairs']} · "
                    f"{r['rows']:,} rows",
                    style="green",
                )
            )
            rm = eodhd.refresh_market(
                reg.eodhd_market_symbols(), run=True, root=root, full_refresh=full
            )
            console.print(
                Text(
                    f"EODHD market: {rm['symbols_with_data']}/{rm['symbols']} · "
                    f"{rm['rows']:,} rows",
                    style="green",
                )
            )
        except Exception as exc:
            console.print(f"[red]EODHD {type(exc).__name__}[/red]: {exc}")
            rc = 1
    console.print(Text(f"-> {root}", style="dim"))
    return rc


def top_help() -> str:
    """The top-level help, rendered from :data:`COMMANDS`."""
    rows = [(c.synopsis, c.summary) for c in COMMANDS.values()]
    width = max(len(u) for u, _ in rows)
    fetch = COMMANDS["fetch"].flags
    fwidth = max(len(f"{f.name} {f.metavar}".rstrip()) for f in fetch)
    lines = [
        f"{PROG} -- FRED + EODHD macro data adapter for the Raw Data Lab",
        "",
        f"Usage:  {PROG} <command> [flags]      (bare `{PROG}` == `{PROG} status`)",
        "",
        "Commands:",
        *[f"  {u:<{width}}  {s}" for u, s in rows],
        "",
        "fetch flags:",
        *[
            f"  {(f.name + ' ' + f.metavar).rstrip():<{fwidth}}  {f.help}"
            for f in fetch
        ],
        "",
        _KEYS_NOTE,
        "",
        f"Run '{PROG} <command> --help' for the full help.",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"list": cmd_list, "status": cmd_status, "fetch": cmd_fetch}
    if command in ("-h", "--help", "help"):
        if rest and rest[0] in COMMANDS:  # `macro help fetch`
            print(command_help(rest[0]))
        else:
            print(top_help())
        return 0
    if command in dispatch:
        if command == "fetch":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("macro", "fetch", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
