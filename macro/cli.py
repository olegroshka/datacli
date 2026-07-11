"""``macro`` command group: list / status / fetch the FRED + EODHD macro data.

Dry-run by default; ``fetch --run`` performs the actual pull. ``--provider
fred|eodhd|all`` selects the source. Rendered in the shared ``_render`` palette.
"""

from __future__ import annotations

import sys
from pathlib import Path

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


def _flag_value(argv: list[str], flag: str, default: str) -> str:
    for i, token in enumerate(argv):
        if token == flag and i + 1 < len(argv):
            return argv[i + 1]
        if token.startswith(flag + "="):
            return token.split("=", 1)[1]
    return default


def cmd_list(argv: list[str]) -> int:
    """List the registered FRED series and EODHD indicators."""
    from rich.text import Text

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


def cmd_status(argv: list[str]) -> int:
    """Show what macro data is on disk, per provider."""
    console = _render.make_console()
    root = macro_config.macro_root()
    _fred_status(console, root)
    _eodhd_status(console, root)
    return 0


def cmd_fetch(argv: list[str]) -> int:
    """Fetch macro data to parquet (dry-run unless --run). --provider fred|eodhd|all."""
    from rich.text import Text

    console = _render.make_console()
    run = "--run" in argv
    provider = _flag_value(argv, "--provider", "all")
    if provider not in ("fred", "eodhd", "all"):
        console.print(f"[red]unknown provider '{provider}'[/red] (fred|eodhd|all)")
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
                    f"  EODHD  {len(reg.eodhd_pairs())} pairs   "
                    f"key: {'set' if key else 'NOT SET'}",
                    style="" if key else "red",
                )
            )
        console.print(Text("re-run with --run to fetch", style="dim"))
        return 0

    rc = 0
    if do_fred:
        try:
            r = fred.refresh(reg.fred_ids(), run=True, root=root)
            msg = f"FRED: {r['series_with_data']}/{r['series']} series · {r['rows']:,} rows"
            if r["failed"]:
                msg += f" · failed: {', '.join(r['failed'])}"
            console.print(Text(msg, style="green" if not r["failed"] else "yellow"))
        except Exception as exc:
            console.print(f"[red]FRED {type(exc).__name__}[/red]: {exc}")
            rc = 1
    if do_eodhd:
        try:
            r = eodhd.refresh(reg.eodhd_pairs(), run=True, root=root)
            console.print(
                Text(
                    f"EODHD: {r['series_with_data']}/{r['pairs']} series with data · "
                    f"{r['rows']:,} rows",
                    style="green",
                )
            )
        except Exception as exc:
            console.print(f"[red]EODHD {type(exc).__name__}[/red]: {exc}")
            rc = 1
    console.print(Text(f"-> {root}", style="dim"))
    return rc


def top_help() -> str:
    return (
        "macro -- FRED + EODHD macro data adapter for the Raw Data Lab\n\n"
        "Commands:\n"
        "  list                          FRED series + EODHD indicators\n"
        "  status                        What macro data is on disk\n"
        "  fetch [--provider X] [--run]  Fetch (X = fred | eodhd | all; dry-run default)\n"
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
