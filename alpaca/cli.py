"""``alpaca`` command group: status / fetch / qc for the auction prints.

``fetch`` resolves the universe (every US stock and ETF ever in the eodhd
intraday lane), plans what each ticker still needs and, with ``--run``,
reads Alpaca's historical auctions endpoint and stores the prints. Without
``--run`` it only prints the plan: no credentials, no request.
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

import alpaca  # noqa: F401  (puts eodhd/ and the repo root on sys.path)
from alpaca import auth, config, qc  # noqa: E402
from alpaca.client import AuctionsClient, RateLimiter  # noqa: E402
from alpaca.errors import AlpacaError  # noqa: E402
from alpaca.quiet import QuietSchedule  # noqa: E402
from alpaca.refresh import (  # noqa: E402
    CoveragePolicy,
    RefreshReport,
    refresh,
    target_date,
)
from alpaca.store import STATUSES, AuctionStore  # noqa: E402
from alpaca.store_closes import DailyStoreCloses  # noqa: E402
from alpaca.universe import IntradayUniverse  # noqa: E402

PROG = "alpaca"

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "status",
            "What auctions are stored (bare `alpaca` does this)",
            "Show the stored auction prints: tickers by status, coverage, prints,\n"
            "the daily file, the last fetch, and whether a key pair is configured.",
            (Flag("--json", "machine-readable output"),),
        ),
        Command(
            "fetch",
            "Fetch official auction prints (dry run unless --run)",
            "Plan and fetch the opening and closing auction prints of every US stock\n"
            "and ETF ever in the eodhd intraday lane, from 2016 (Alpaca SIP feed).\n"
            "A run tops every ticker up first (re-reading the last few days for late\n"
            "reports), then fills history. Retired symbols are asked for as of their\n"
            "last daily price. Nothing starts inside a quiet window ([alpaca].quiet_windows,\n"
            "default 15:30-16:15 New York on weekdays: the key is shared with the paper book).\n"
            "WITHOUT --run nothing is sent and nothing is written.",
            (
                Flag("--run", "fetch and store (default is a dry run)"),
                Flag("--full", "re-read every ticker's whole span"),
                Flag(
                    "--tickers",
                    "only these store tickers (comma-separated)",
                    metavar="LIST",
                ),
                Flag(
                    "--to",
                    "newest session to ask for (default: the last final one)",
                    metavar="DATE",
                ),
            ),
        ),
        Command(
            "qc",
            "Quality checks over the stored auctions",
            "Check that files and state agree, that no ticker is in error and that the\n"
            "newest coverage is recent. --crosscheck also measures the eodhd intraday\n"
            "lane's closing cross estimate and the eodhd daily open against the\n"
            "official crosses (local, no network).",
            (
                Flag(
                    "--crosscheck",
                    "compare with the eodhd daily open and cross estimate",
                ),
            ),
        ),
    )
}


def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    return "\n".join(
        [
            f"{PROG} -- Alpaca market data: official opening and closing auction prints (SIP, from 2016)",
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


def _store() -> AuctionStore:
    return AuctionStore(config.alpaca_root())


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _client(settings: config.Settings) -> AuctionsClient:
    return AuctionsClient(
        auth.Credentials.from_env(),
        data_url=settings.data_url,
        feed=settings.feed,
        page_limit=settings.page_limit,
        max_pages=settings.max_pages,
        timeout=settings.timeout_seconds,
        limiter=RateLimiter(settings.max_requests_per_minute),
    )


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def status_payload() -> dict[str, Any]:
    root, source = config.resolve_root()
    store = AuctionStore(root)
    states = store.load_state()
    counts = {name: 0 for name in STATUSES}
    for state in states.values():
        counts[state.status] = counts.get(state.status, 0) + 1
    firsts = [s.first_data for s in states.values() if s.first_data]
    lasts = [s.last_data for s in states.values() if s.last_data]
    fetched = [s.fetched_at for s in states.values() if s.fetched_at]
    daily_rows = None
    if store.daily_path.exists():
        import pyarrow.parquet as pq

        daily_rows = pq.ParquetFile(store.daily_path).metadata.num_rows
    coverage = store.read_coverage()
    thin = (
        sorted(coverage.loc[coverage["status"] == "thin", "ticker"])
        if not coverage.empty
        else []
    )
    return {
        "root": str(root),
        "root_source": source,
        "key_configured": auth.available(),
        "tickers": len({s.ticker for s in states.values()}),
        "units": len(states),
        "aliases": sum(1 for s in states.values() if s.role == "alias"),
        "by_status": counts,
        "coverage": {
            "ok": int(len(coverage) - len(thin)),
            "thin": len(thin),
            "thin_tickers": thin,
        },
        "retired": sum(
            1 for s in states.values() if s.role == "main" and s.asof is not None
        ),
        "first_data": min(firsts).isoformat() if firsts else None,
        "last_data": max(lasts).isoformat() if lasts else None,
        "prints": sum(s.n_prints for s in states.values()),
        "ticker_days": sum(s.n_days for s in states.values()),
        "daily_rows": daily_rows,
        "last_fetch": max(fetched) if fetched else None,
    }


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
    key = (
        "configured"
        if payload["key_configured"]
        else "missing (APCA_API_KEY_ID / APCA_API_SECRET_KEY)"
    )
    console.print(f"key pair: {key}")
    if not payload["tickers"]:
        console.print(
            Text(
                "nothing stored yet -- run:  alpaca fetch  (plan), then  alpaca fetch --run",
                style="dim",
            )
        )
        return 0
    by = payload["by_status"]
    daily = payload["daily_rows"]
    rows = [
        (
            "tickers",
            f"{_render.fmt_int(payload['tickers'])} ({payload['retired']} retired, asked as of their last price)",
        ),
        (
            "fetch units",
            f"{payload['units']} ({payload['aliases']} reviewed aliases): "
            + ", ".join(f"{by.get(name, 0)} {name}" for name in STATUSES),
        ),
        ("coverage", _coverage_line(payload["coverage"])),
        (
            "prints",
            f"{payload['first_data'] or '-'} .. {payload['last_data'] or '-'}, {_render.fmt_int(payload['prints'])} prints",
        ),
        (
            "daily file",
            f"{_render.fmt_int(daily)} ticker-days" if daily is not None else "-",
        ),
        ("last fetch", (payload["last_fetch"] or "")[:16].replace("T", " ") or "-"),
    ]
    table = _render.minimal_table(title="auctions", show_header=False)
    table.add_column("", no_wrap=True)
    table.add_column("")
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)
    return 0


def _coverage_line(coverage: dict[str, Any]) -> str:
    if not coverage["ok"] and not coverage["thin"]:
        return "not assessed yet (alpaca fetch --run)"
    line = f"{coverage['ok']} ok, {coverage['thin']} thin"
    thin = coverage["thin_tickers"]
    if thin:
        line += f" ({', '.join(thin[:8])}{', ...' if len(thin) > 8 else ''})"
    return line


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def cmd_fetch(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(Text(f"fetch takes flags only, got {rest}", style="red"))
        return 2
    try:
        settings = config.load_settings()
        target = (
            dt.date.fromisoformat(str(flags["--to"]))
            if "--to" in flags
            else target_date(_now())
        )
        quiet = QuietSchedule(settings.quiet_windows, settings.quiet_timezone)
    except ValueError as exc:
        console.print(Text(str(exc), style="red"))
        return 2
    tickers = (
        [t.strip().upper() for t in str(flags["--tickers"]).split(",") if t.strip()]
        if "--tickers" in flags
        else None
    )
    universe = IntradayUniverse(
        config.eodhd_root(),
        history_start=settings.history_start,
        retired_after_days=settings.retired_after_days,
        symbol_map=settings.symbol_map,
        tickers=tickers,
    )
    run = "--run" in flags
    if run:
        window = quiet.active(_now())
        if window is not None:
            console.print(
                Text(
                    f"inside the quiet window {window} {quiet.timezone}: nothing sent (the key is shared)",
                    style="yellow",
                )
            )
            return 1
        if not auth.available():
            console.print(
                Text(
                    f"{auth.KEY_ID_VAR} / {auth.SECRET_VAR} not set: nothing sent",
                    style="red",
                )
            )
            return 1
    try:
        report = refresh(
            universe,
            _store(),
            target=target,
            run=run,
            overlap_days=settings.overlap_days,
            per_request=settings.symbols_per_request,
            page_limit=settings.page_limit,
            full="--full" in flags,
            client_factory=lambda: _client(settings),
            closes=DailyStoreCloses(config.eodhd_root()),
            policy=CoveragePolicy(
                min_coverage=settings.min_coverage,
                max_gap_sessions=settings.max_gap_sessions,
                alias_tolerance=settings.alias_tolerance,
                alias_min_match=settings.alias_min_match,
            ),
            quiet=quiet,
            now=_now,
        )
    except FileNotFoundError as exc:
        console.print(Text(str(exc), style="red"))
        return 1
    except AlpacaError as exc:
        console.print(Text(f"stopped: {exc}", style="red"))
        return 1
    _print_report(console, report)
    if report.failed or report.stopped or report.refused:
        return 1
    return 0


def _print_report(console: Any, report: RefreshReport) -> None:
    from rich.text import Text

    verb = "fetched" if report.run else "plan (dry run)"
    console.print(
        Text(
            f"auctions {verb}: target {report.target}, {report.tickers} tickers in {report.spans} fetch units",
            style="bold",
        )
    )
    if report.skipped:
        sample = ", ".join(
            f"{t} ({why})" for t, why in list(report.skipped.items())[:5]
        )
        console.print(Text(f"  skipped {len(report.skipped)}: {sample}", style="dim"))
    if report.in_sync:
        print("Everything in sync.")
        _print_coverage(console, report)
        return
    kinds: dict[str, int] = {}
    for task in report.tasks:
        kinds[task.kind] = kinds.get(task.kind, 0) + 1
    detail = ", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))
    console.print(
        f"  {len(report.tasks)} tasks ({detail}) in {len(report.batches)} requests, about {report.estimated_pages} pages"
    )
    if not report.run:
        console.print(Text("  nothing sent -- add --run to fetch", style="dim"))
        return
    done = len(report.outcomes)
    console.print(
        f"  {done} of {len(report.batches)} requests done: {report.requests} pages, "
        f"{_render.fmt_int(sum(o.days for o in report.outcomes))} ticker-days, "
        f"{_render.fmt_int(sum(o.prints for o in report.outcomes))} prints"
    )
    for outcome in report.failed[:5]:
        b = outcome.batch
        console.print(
            Text(
                f"  failed {b.start}..{b.end} ({len(b.tasks)} tickers): {outcome.error}",
                style="red",
            )
        )
    if report.stopped:
        console.print(
            Text(f"  stopped before the next request: {report.stopped}", style="yellow")
        )
    for unit in report.refused[:5]:
        console.print(
            Text(
                f"  refused alias {unit}: its closes do not match the store (see the state detail)",
                style="red",
            )
        )
    if report.pruned:
        console.print(
            f"  pruned {_render.fmt_int(report.pruned)} prints outside the current units"
        )
    if report.daily_rows is not None:
        console.print(
            f"  daily file rebuilt: {_render.fmt_int(report.daily_rows)} ticker-days"
        )
    _print_coverage(console, report)


def _print_coverage(console: Any, report: RefreshReport) -> None:
    from rich.text import Text

    if report.coverage is None or report.coverage.empty:
        return
    thin = report.thin()
    line = "  coverage: " + _coverage_line(
        {
            "ok": len(report.coverage) - len(thin),
            "thin": len(thin),
            "thin_tickers": thin,
        }
    )
    console.print(
        Text(
            line + ("  (see alpaca qc)" if thin else ""), style="yellow" if thin else ""
        )
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
    store = _store()
    found = qc.findings(store, target=target_date(_now()))
    if "--crosscheck" in flags:
        ratios, opens = qc.crosscheck(store, config.eodhd_root())
        for r in ratios:
            console.print(
                f"{r.label}: n {_render.fmt_int(r.n)}, median {r.median:.3f}, p10 {r.p10:.3f}, "
                f"p90 {r.p90:.3f}, within 5% {r.within_5pct:.0%}"
            )
        if opens is not None:
            console.print(
                f"eodhd daily open vs official opening cross: n {_render.fmt_int(opens.n)}, "
                f"differ (> half a cent) {opens.differ_share:.0%}, median |difference| {opens.median_abs_bp:.1f} bp"
            )
    if not found:
        console.print(Text("auctions: no findings", style="green"))
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
    dispatch = {"status": cmd_status, "fetch": cmd_fetch, "qc": cmd_qc}
    if command in ("-h", "--help", "help"):
        print(command_help(rest[0]) if rest and rest[0] in COMMANDS else top_help())
        return 0
    if command in dispatch:
        if command == "fetch":
            from scheduler.commands import direct_mutation_lock
            from scheduler.locks import LockUnavailable

            try:
                with direct_mutation_lock("alpaca", "fetch", rest):
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
