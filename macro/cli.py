"""``macro`` command group: list / status / fetch the FRED macro series.

Dry-run by default; ``fetch --run`` performs the actual pull (needs FRED_API_KEY).
Rendered in the shared eodhd ``_render`` palette.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from macro import config as macro_config  # noqa: E402
from macro import fred  # noqa: E402
from macro import registry as reg  # noqa: E402

_EODHD = _REPO / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
import _render  # type: ignore[import-not-found]  # noqa: E402


def cmd_list(argv: list[str]) -> int:
    """List the registered macro series."""
    console = _render.make_console()
    table = _render.boxed_table(title=f"macro series ({len(reg.SERIES)})")
    table.add_column("series_id", style="cyan", no_wrap=True)
    table.add_column("name")
    table.add_column("category", no_wrap=True)
    for s in reg.SERIES.values():
        table.add_row(s.series_id, s.name, s.category)
    console.print(table)
    return 0


def cmd_status(argv: list[str]) -> int:
    """Show which series are on disk and their coverage."""
    from rich.text import Text

    console = _render.make_console()
    root = macro_config.macro_root()
    frame = fred.load(root)
    if frame is None or frame.empty:
        console.print(
            f"[yellow]no macro data[/yellow] at {root}\n"
            "fetch it with:  macro fetch --run   (needs FRED_API_KEY)"
        )
        return 0

    stats = (
        frame.groupby("series_id")["date"].agg(["size", "min", "max"]).to_dict("index")
    )
    table = _render.minimal_table(
        title=f"macro status · {frame['series_id'].nunique()} series · "
        f"{len(frame):,} rows"
    )
    table.add_column("series_id", style="cyan", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("rows", justify="right", no_wrap=True)
    table.add_column("first", no_wrap=True)
    table.add_column("last", no_wrap=True)
    for sid, s in reg.SERIES.items():
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
    console.print(table)
    return 0


def cmd_fetch(argv: list[str]) -> int:
    """Fetch all registered series to parquet (dry-run unless --run)."""
    from rich.text import Text

    console = _render.make_console()
    run = "--run" in argv
    root = macro_config.macro_root()
    have_key = bool(fred.api_key())

    if not run:
        console.print(
            Text(f"plan: fetch {len(reg.SERIES)} FRED series -> {root}", style="bold")
        )
        console.print(
            Text(
                f"FRED_API_KEY: {'set' if have_key else 'NOT SET (free at fred.stlouisfed.org)'}",
                style="green" if have_key else "red",
            )
        )
        console.print(
            Text("re-run with --run to fetch (hits the FRED API)", style="dim")
        )
        return 0

    try:
        result = fred.refresh(reg.series_ids(), run=True, root=root)
    except Exception as exc:
        console.print(f"[red]{type(exc).__name__}[/red]: {exc}")
        return 1
    console.print(
        Text(
            f"fetched {result['series']} series · {result['rows']:,} rows -> {result['path']}",
            style="green",
        )
    )
    return 0


def top_help() -> str:
    return (
        "macro -- FRED macro data adapter for the Raw Data Lab\n\n"
        "Commands:\n"
        "  list             List the registered macro series\n"
        "  status           What macro data is on disk\n"
        "  fetch [--run]    Fetch series to parquet (dry-run unless --run)\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_status([])
    command, rest = args[0], args[1:]
    dispatch = {"list": cmd_list, "status": cmd_status, "fetch": cmd_fetch}
    if command in ("-h", "--help", "help"):
        print(top_help())
        return 0
    if command in dispatch:
        return dispatch[command](rest)
    print(f"unknown macro command: {command!r}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
