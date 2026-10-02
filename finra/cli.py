"""``finra`` command group: list / status / fetch / qc / probe the FINRA source.

Dry-run by default; ``fetch --run`` performs the actual pull under the same
mutation lock the scheduler uses. ``probe`` and ``status --live`` reach the
network read-only; everything else is offline. Rendered in the shared
``_render`` palette; see ``docs/FINRA_SOURCE_DESIGN.md``.
"""

from __future__ import annotations

import difflib
import json as jsonlib
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import _cmdtable as ct  # type: ignore[import-not-found]  # noqa: E402
import _render  # type: ignore[import-not-found]  # noqa: E402
from _cmdtable import Command, Flag  # type: ignore[import-not-found]  # noqa: E402

import finra  # noqa: E402,F401  (puts eodhd/ on sys.path)
from finra import config as finra_config  # noqa: E402
from finra import registry as reg  # noqa: E402
from finra import short_volume as sv  # noqa: E402
from finra import weekly_flow as wf  # noqa: E402
from finra import short_interest as si  # noqa: E402
from finra.api import QueryApiClient  # noqa: E402
from finra.auth import (  # noqa: E402
    CLIENT_ID_VAR,
    SECRET_VAR,
    Credentials,
    TokenProvider,
    mask,
)
from finra.cdn import DailyFileClient  # noqa: E402
from finra.errors import FinraError  # noqa: E402

PROG = "finra"
PROBES = ("auth", "metadata", "partitions")
LIVE_TIMEOUT = 15.0

_KEYS_NOTE = (
    f"Credentials: {CLIENT_ID_VAR} and {SECRET_VAR} (the client secret), read from\n"
    "the environment or the Windows user environment, never stored. Public\n"
    "datasets (daily short volume) work without them."
)

COMMANDS: dict[str, Command] = {
    c.name: replace(c, prog=PROG)
    for c in (
        Command(
            "list",
            "Datasets the finra source knows about",
            "List the registered FINRA datasets: how each is addressed on the Query\n"
            "API, which public file family (if any) routine fetches read, the first\n"
            "available date and whether credentials are needed. Touches nothing.",
        ),
        Command(
            "status",
            "What FINRA data is on disk (bare `finra` does this)",
            "Show the resolved data root, whether credentials are present (masked),\n"
            "and what each dataset holds on disk: days stored, first/last day and the\n"
            "fetch-state counts. Offline by default; --live also asks the Query API\n"
            "which partitions FINRA has published, as a separate fact.",
            (
                Flag(
                    "--live",
                    "also report what FINRA has published (one API call per dataset)",
                ),
                Flag("--json", "machine-readable output instead of tables"),
            ),
        ),
        Command(
            "fetch",
            "Fetch daily short volume to parquet (dry-run plan unless --run)",
            "Plan the trade dates to fetch and, with --run, fetch them newest first:\n"
            "every weekday FINRA is due to have published (18:00 ET on the trade\n"
            "date) that is not stored yet, plus the trailing --overlap-days weekdays,\n"
            "which are re-checked for restated files and late publications. Each day\n"
            "is written and recorded before the next, so an interrupted backfill\n"
            "resumes. WITHOUT --run nothing is touched.\n\n"
            "The default transport is FINRA's public consolidated file (no\n"
            f"credentials, history from {sv.SPEC.first_date}); `--transport api` reads the\n"
            "Query API instead (per-facility rows summed, rolling one-year window).\n\n"
            f"{_KEYS_NOTE}",
            (
                Flag(
                    "--dataset",
                    "dataset to fetch: short_volume | weekly_flow | short_interest (default: short_volume)",
                    metavar="<name>",
                ),
                Flag(
                    "--from",
                    f"first trade date, YYYY-MM-DD (default: {sv.SPEC.first_date})",
                    metavar="<date>",
                ),
                Flag(
                    "--to",
                    "last trade date, YYYY-MM-DD (default: today)",
                    metavar="<date>",
                ),
                Flag(
                    "--limit-days",
                    "fetch at most this many days (newest first)",
                    metavar="<n>",
                ),
                Flag(
                    "--overlap-days",
                    f"weekdays re-checked at the end of the range (default: {sv.DEFAULT_OVERLAP_DAYS})",
                    metavar="<n>",
                ),
                Flag(
                    "--retry-absent",
                    "also re-probe days recorded as absent outside the overlap",
                ),
                Flag(
                    "--transport",
                    f"{' | '.join(sv.SOURCES)} (default: cdn; weekly_flow is api only)",
                    metavar="<name>",
                ),
                Flag("--full", "re-fetch every day in the range, stored or not"),
                Flag("--run", "actually fetch (default is a dry-run plan)"),
            ),
        ),
        Command(
            "qc",
            "Quality checks over the stored days (offline)",
            "Check every stored day for duplicate symbols, short > total, exempt >\n"
            "short, negative volumes and a date mismatch; reconcile the files with\n"
            "the fetch-state sidecar; list weekday gaps that are neither stored nor\n"
            "recorded absent; and, when the EODHD us_common universe is on disk,\n"
            "report how many of its common codes have no FINRA row on the latest day.\n"
            "Exit 1 on any error-severity finding.",
            (
                Flag(
                    "--dataset",
                    "dataset to check (default: short_volume)",
                    metavar="<name>",
                ),
            ),
        ),
        Command(
            "probe",
            "Live read-only calls: auth | metadata <dataset> | partitions <dataset>",
            "Exercise the access layer against the real API, read-only:\n"
            "  probe auth                   obtain a bearer token (prints its lifetime only)\n"
            "  probe metadata <dataset>     the dataset's fields and partition fields\n"
            "  probe partitions <dataset>   the partitions FINRA has published\n"
            "<dataset> is a registry name (see `finra list`); --group/--name address\n"
            f"any other dataset directly.\n\n{_KEYS_NOTE}",
            (
                Flag("--group", "API dataset group, e.g. otcMarket", metavar="<group>"),
                Flag(
                    "--name",
                    "API dataset name, e.g. consolidatedShortInterest",
                    metavar="<name>",
                ),
            ),
            positionals="<what> [<dataset>]",
        ),
    )
}


def _args(
    command: str, argv: list[str]
) -> tuple[dict[str, Any], list[str], int | None]:
    return ct.parse_or_exit(COMMANDS[command], argv, console=_render.make_console())


# --------------------------------------------------------------------------- #
# access-layer construction (one place; tests swap these factories)
# --------------------------------------------------------------------------- #
def make_session() -> Any:
    import requests

    return requests.Session()


def credentials() -> Credentials | None:
    """The configured credential pair, or ``None`` (``CredentialsError`` if half set)."""
    return Credentials.from_env()


def make_client(*, timeout: float | None = None) -> QueryApiClient:
    """A client with a token provider when credentials are configured."""
    session = make_session()
    creds = credentials()
    provider = TokenProvider(session, creds) if creds else None
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return QueryApiClient(session, provider, **kwargs)


#: dataset name -> provider module (store / refresh / qc / SPEC / DEFAULT_OVERLAP_DAYS)
PROVIDERS: dict[str, Any] = {sv.SPEC.name: sv, wf.SPEC.name: wf, si.SPEC.name: si}


def transports_for(spec: reg.DatasetSpec) -> tuple[str, ...]:
    """The transports a dataset accepts; the first is its default."""
    return sv.SOURCES if spec.cdn_family else (sv.SOURCE_API,)


def make_transport(spec: reg.DatasetSpec, name: str) -> Any:
    """Every provider exposes ``ApiTransport``; only short volume has a CDN one."""
    if name == sv.SOURCE_API:
        return PROVIDERS[spec.name].ApiTransport(make_client())
    return sv.CdnTransport(DailyFileClient(make_session()))


def eodhd_common_codes() -> list[str] | None:
    """EODHD ``Code`` values of the us_common universe, or ``None`` when not on disk."""
    try:
        import config as eodhd_config  # type: ignore[import-not-found]
        import pandas as pd

        root, _ = eodhd_config.eodhd_data_root()
        path = Path(root) / "us_common" / "tickers_US.parquet"
        if not path.exists():
            return None
        frame = pd.read_parquet(path, columns=["Code", "Type"])
        common = frame[frame["Type"].astype(str) == "Common Stock"]
        return sorted(common["Code"].astype(str).unique())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# list
# --------------------------------------------------------------------------- #
def cmd_list(argv: list[str]) -> int:
    _, _, done = _args("list", argv)
    if done is not None:
        return done
    console = _render.make_console()
    table = _render.boxed_table(title=f"FINRA datasets ({len(reg.DATASETS)})")
    table.add_column("dataset", style="cyan", no_wrap=True)
    table.add_column("transport", no_wrap=True)
    table.add_column("first date", no_wrap=True)
    table.add_column("credentials", no_wrap=True)
    table.add_column("api", no_wrap=True)
    table.add_column(
        "summary"
    )  # last and wrapping, so a narrow console squeezes it, not the keys
    for s in reg.DATASETS.values():
        table.add_row(
            s.name,
            s.transport,
            s.first_date.isoformat(),
            "required" if s.entitled else "optional",
            f"{s.api_group}/{s.api_name}",
            s.summary,
        )
    console.print(table)
    return 0


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #
def _dataset_disk_status(spec: reg.DatasetSpec, root: Path) -> dict[str, Any]:
    """Offline facts about one dataset: its partition files and state sidecar."""
    day_store = PROVIDERS[spec.name].store(root)
    days = sorted({day for day, _part in day_store.partitions_on_disk()})
    state = day_store.load_state()
    counts: dict[str, int] = {}
    for st in state.values():
        counts[st.status] = counts.get(st.status, 0) + 1
    latest_fetch = max((st.fetched_at for st in state.values()), default="")
    return {
        "dataset": spec.name,
        "dir": str(day_store.dir),
        "present": bool(days),
        "days": len(days),
        "first": days[0].isoformat() if days else None,
        "last": days[-1].isoformat() if days else None,
        "state": counts,
        "last_fetch": latest_fetch,
    }


def _published(client: QueryApiClient, spec: reg.DatasetSpec) -> dict[str, Any]:
    """What FINRA has published for ``spec``: a separate plane from what is on disk."""
    try:
        parts = client.partitions(spec.api_group, spec.api_name)
    except FinraError as exc:
        return {"reachable": False, "error": str(exc)}
    except Exception as exc:  # network down is a fact to report, not a crash
        return {"reachable": False, "error": f"{type(exc).__name__}: {exc}"}
    return {
        "reachable": True,
        "partitions": len(parts),
        "first": parts[0] if parts else None,
        "last": parts[-1] if parts else None,
    }


def collect_status(*, live: bool = False) -> dict[str, Any]:
    """The status facts as plain data (rendered by :func:`cmd_status`)."""
    root, source = finra_config.resolve_root()
    try:
        creds = credentials()
        cred_info: dict[str, Any] = {
            "configured": creds is not None,
            "client_id": mask(creds.client_id) if creds else "",
            "error": "",
        }
    except FinraError as exc:
        cred_info = {"configured": False, "client_id": "", "error": str(exc)}
    datasets = [_dataset_disk_status(s, root) for s in reg.DATASETS.values()]
    if live:
        client = make_client(timeout=LIVE_TIMEOUT)
        for entry, s in zip(datasets, reg.DATASETS.values()):
            entry["published"] = _published(client, s)
    return {
        "root": str(root),
        "root_source": source,
        "credentials": cred_info,
        "datasets": datasets,
    }


def cmd_status(argv: list[str]) -> int:
    from rich.text import Text

    flags, _, done = _args("status", argv)
    if done is not None:
        return done
    status = collect_status(live="--live" in flags)
    if "--json" in flags:
        print(jsonlib.dumps(status, indent=2))
        return 0
    console = _render.make_console()
    console.print(
        Text(f"root: {status['root']}  ({status['root_source']})", style="bold")
    )
    creds = status["credentials"]
    if creds["error"]:
        console.print(Text(f"credentials: {creds['error']}", style="red"))
    elif creds["configured"]:
        console.print(
            Text(f"credentials: {CLIENT_ID_VAR}={creds['client_id']}  (secret set)")
        )
    else:
        console.print(
            Text(
                f"credentials: not set ({CLIENT_ID_VAR}, {SECRET_VAR}); public datasets only",
                style="dim",
            )
        )
    table = _render.minimal_table(title="datasets")
    table.add_column("dataset", style="cyan", no_wrap=True)
    table.add_column("days", justify="right", no_wrap=True)
    table.add_column("first", no_wrap=True)
    table.add_column("last", no_wrap=True)
    table.add_column("state", no_wrap=True)
    table.add_column("last fetch", no_wrap=True)
    if "--live" in flags:
        table.add_column("published (FINRA)", no_wrap=True)
    for entry in status["datasets"]:
        cells: list[Any] = [
            entry["dataset"],
            str(entry["days"]) if entry["present"] else Text("-", style="dim"),
            entry["first"] or Text("-", style="dim"),
            entry["last"] or Text("-", style="dim"),
            _render.state_cell(entry["state"]),
            (entry["last_fetch"] or "")[:16].replace("T", " ")
            or Text("-", style="dim"),
        ]
        if "--live" in flags:
            pub = entry["published"]
            if pub["reachable"]:
                cells.append(
                    f"{pub['partitions']} partitions · {pub['first']} .. {pub['last']}"
                )
            else:
                cells.append(Text(f"unreachable: {pub['error']}", style="yellow"))
        table.add_row(*cells)
    console.print(table)
    if not any(e["present"] for e in status["datasets"]):
        console.print(
            Text("no FINRA data yet — fetch with:  finra fetch --run", style="dim")
        )
    return 0


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #
def _parse_date(flag: str, value: Any) -> date | None:
    """``date`` from a flag value, ``None`` when unset; ``UsageError`` otherwise."""
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise ct.UsageError(f"{flag} must be YYYY-MM-DD, got {value!r}") from None


def _parse_int(flag: str, value: Any, *, minimum: int) -> int | None:
    if value is None:
        return None
    try:
        number = int(str(value))
    except ValueError:
        number = minimum - 1
    if number < minimum:
        raise ct.UsageError(f"{flag} must be an integer >= {minimum}, got {value!r}")
    return number


def _dataset_flag(console: Any, flags: dict[str, Any]) -> reg.DatasetSpec | None:
    name = str(flags.get("--dataset", sv.SPEC.name))
    try:
        return reg.spec(name)
    except KeyError:
        ct.bad_choice(console, "dataset", name, tuple(reg.DATASETS))
        return None


def cmd_fetch(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("fetch", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if rest:
        console.print(f"[red]fetch takes flags only, got {' '.join(rest)!r}[/red]")
        return 2
    spec = _dataset_flag(console, flags)
    if spec is None:
        return 2
    provider = PROVIDERS[spec.name]
    allowed = transports_for(spec)
    transport_name = str(flags.get("--transport", allowed[0]))
    if transport_name not in allowed:
        return ct.bad_choice(
            console, f"transport for {spec.name}", transport_name, allowed
        )
    try:
        from_date = _parse_date("--from", flags.get("--from"))
        to_date = _parse_date("--to", flags.get("--to"))
        limit_days = _parse_int("--limit-days", flags.get("--limit-days"), minimum=0)
        overlap = _parse_int("--overlap-days", flags.get("--overlap-days"), minimum=0)
    except ct.UsageError as exc:
        console.print(f"[red]{exc}[/red]")
        return 2
    if from_date and to_date and to_date < from_date:
        console.print("[red]--to is before --from[/red]")
        return 2
    if from_date and from_date < spec.first_date:
        console.print(
            Text(
                f"note: --from clamped to the first available date {spec.first_date}",
                style="dim",
            )
        )
        from_date = spec.first_date

    run = "--run" in flags
    root = finra_config.finra_root()
    transport = (
        make_transport(spec, transport_name) if run else _DryTransport(transport_name)
    )
    try:
        report = provider.refresh(
            run=run,
            root=root,
            transport=transport,
            from_date=from_date,
            to_date=to_date,
            limit_days=limit_days,
            overlap_days=(
                overlap if overlap is not None else provider.DEFAULT_OVERLAP_DAYS
            ),
            retry_absent="--retry-absent" in flags,
            full_refresh="--full" in flags,
            progress=lambda line: (
                console.print(Text(f"  {line}", style="dim")) if run else None
            ),
        )
    except FinraError as exc:
        console.print(Text(f"{type(exc).__name__}: {exc}", style="red"))
        return 1
    _render_report(console, report)
    return 0 if report.ok else 1


class _DryTransport:
    """A transport that is never called: a dry run must not touch the network."""

    def __init__(self, name: str) -> None:
        self.name = name

    def fetch_day(self, trade_date: date) -> Any:
        raise AssertionError("dry run must not fetch")

    def fetch_partition(self, week_start: date, tier: str) -> Any:
        raise AssertionError("dry run must not fetch")


def _render_report(console: Any, report: Any) -> None:
    """Render a refresh report; both providers' reports share these attributes."""
    from rich.text import Text

    if not report.run:
        console.print(
            Text(f"plan -> {report.root}  (transport: {report.source})", style="bold")
        )
        if report.planned:
            console.print(
                f"  {len(report.planned)} partition(s) to fetch, newest first: "
                f"{report.planned[0]} .. {report.planned[-1]}"
            )
        else:
            console.print(
                Text("  nothing to fetch: the store is current", style="green")
            )
        if report.skipped:
            console.print(
                Text(
                    f"  {len(report.skipped)} more partition(s) left for later by --limit-days",
                    style="dim",
                )
            )
        if report.not_yet_publishable:
            console.print(
                Text(
                    f"  {report.not_yet_publishable} is not publishable yet (FINRA posts by 18:00 ET)",
                    style="dim",
                )
            )
        console.print(Text("re-run with --run to fetch", style="dim"))
        return
    parts = [f"stored {len(report.stored)}"]
    if report.restated:
        parts.append(f"restated {len(report.restated)}")
    if report.absent:
        parts.append(f"absent {len(report.absent)}")
    if report.failed:
        parts.append(f"failed {len(report.failed)}")
    style = "green" if report.ok else ("red" if report.aborted else "yellow")
    console.print(
        Text(
            f"{report.dataset}: {' · '.join(parts)} · {report.rows:,} rows  ({report.source})",
            style=style,
        )
    )
    if report.aborted:
        console.print(Text(f"aborted: {report.aborted}", style="red"))
    for day, why in report.failed[:10]:
        console.print(Text(f"  {day}: {why}", style="red"))
    if report.skipped:
        console.print(
            Text(
                f"{len(report.skipped)} more partition(s) left for the next run",
                style="dim",
            )
        )
    console.print(Text(f"-> {report.root}", style="dim"))


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
        console.print(f"[red]qc takes flags only, got {' '.join(rest)!r}[/red]")
        return 2
    spec = _dataset_flag(console, flags)
    if spec is None:
        return 2
    report = PROVIDERS[spec.name].qc(
        finra_config.finra_root(), eodhd_codes=eodhd_common_codes()
    )
    span = f"{report.first} .. {report.last}" if report.days else "nothing"
    console.print(
        Text(
            f"{report.dataset}: {report.days} partition(s) on disk · {span}",
            style="bold",
        )
    )
    if not report.findings:
        console.print(Text("no findings", style="green"))
    for f in report.findings:
        token = _render.severity_cell(f.severity)
        line = Text()
        line.append_text(token)
        line.append(f"  {f.check}: {f.message}")
        console.print(line)
    return 0 if report.ok else 1


# --------------------------------------------------------------------------- #
# probe
# --------------------------------------------------------------------------- #
def _resolve_target(
    flags: dict[str, Any], rest: list[str], console: Any
) -> tuple[str, str] | None:
    """``(group, name)`` from a registry dataset or ``--group/--name``; ``None`` on a usage error."""
    group, name = flags.get("--group"), flags.get("--name")
    if rest:
        if group or name:
            console.print(
                "[red]give either a dataset name or --group/--name, not both[/red]"
            )
            return None
        try:
            s = reg.spec(rest[0])
        except KeyError:
            ct.bad_choice(console, "dataset", rest[0], tuple(reg.DATASETS))
            return None
        return s.api_group, s.api_name
    if group and name:
        return str(group), str(name)
    console.print("[red]probe needs a dataset name or both --group and --name[/red]")
    return None


def cmd_probe(argv: list[str]) -> int:
    from rich.text import Text

    flags, rest, done = _args("probe", argv)
    if done is not None:
        return done
    console = _render.make_console()
    if not rest:
        console.print(f"[red]probe needs one of: {', '.join(PROBES)}[/red]")
        return 2
    what, rest = rest[0], rest[1:]
    if what not in PROBES:
        return ct.bad_choice(console, "probe", what, PROBES)
    try:
        if what == "auth":
            return _probe_auth(console)
        target = _resolve_target(flags, rest, console)
        if target is None:
            return 2
        client = make_client()
        group, name = target
        if what == "metadata":
            return _probe_metadata(console, client, group, name)
        return _probe_partitions(console, client, group, name)
    except FinraError as exc:
        console.print(Text(f"{type(exc).__name__}: {exc}", style="red"))
        return 1


def _probe_auth(console: Any) -> int:
    from rich.text import Text

    creds = credentials()
    if creds is None:
        console.print(
            Text(f"credentials not set ({CLIENT_ID_VAR}, {SECRET_VAR})", style="red")
        )
        return 1
    provider = TokenProvider(make_session(), creds)
    provider.token()
    remaining = provider.seconds_remaining() or 0.0
    console.print(
        Text(
            f"token ok for {CLIENT_ID_VAR}={mask(creds.client_id)} · valid {remaining / 3600:.1f} h",
            style="green",
        )
    )
    return 0


def _probe_metadata(console: Any, client: QueryApiClient, group: str, name: str) -> int:
    from rich.text import Text

    meta = client.metadata(group, name)
    # One field per block (name · type · format, then the description indented)
    # rather than a table: FINRA field names run to 46 characters and the
    # descriptions to sentences, which no column layout survives at 80 columns.
    title = f"{group}/{name} · {len(meta.fields)} fields"
    if meta.description:
        title += f" · {meta.description}"
    console.print(Text(title, style="bold"))
    console.print(
        Text(f"partitioned by: {', '.join(meta.partition_fields) or '-'}", style="dim")
    )
    for f in meta.fields:
        line = Text(f.name, style="cyan")
        line.append(f"  {f.type}", style="")
        if f.format:
            line.append(f"  {f.format}", style="dim")
        console.print(line)
        if f.description:
            console.print(Text(f"    {f.description}", style="dim"))
    return 0


def _probe_partitions(
    console: Any, client: QueryApiClient, group: str, name: str
) -> int:
    from rich.text import Text

    parts = client.partitions(group, name)
    if not parts:
        console.print(Text(f"{group}/{name}: no partitions published", style="yellow"))
        return 0
    console.print(
        Text(
            f"{group}/{name}: {len(parts)} partitions · {parts[0]} .. {parts[-1]}",
            style="bold",
        )
    )
    console.print(Text("latest: " + ", ".join(parts[-5:]), style="dim"))
    return 0


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #
def command_help(name: str) -> str:
    return ct.command_help(COMMANDS[name])


def top_help() -> str:
    lines = [
        f"{PROG} -- FINRA Query API + public daily files (short volume first)",
        "",
        f"Usage:  {PROG} <command> [flags]      (bare `{PROG}` == `{PROG} status`)",
        "",
        "Commands:",
        *ct.commands_block(COMMANDS),
        "",
        "fetch flags:",
        *ct.flags_block(COMMANDS["fetch"].flags),
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
    dispatch = {
        "list": cmd_list,
        "status": cmd_status,
        "fetch": cmd_fetch,
        "qc": cmd_qc,
        "probe": cmd_probe,
    }
    if command in ("-h", "--help", "help"):
        if rest and rest[0] in COMMANDS:
            print(command_help(rest[0]))
        else:
            print(top_help())
        return 0
    if command in dispatch:
        if command == "fetch":
            from scheduler.commands import direct_mutation_lock

            with direct_mutation_lock("finra", "fetch", rest):
                return dispatch[command](rest)
        return dispatch[command](rest)
    near = difflib.get_close_matches(command, list(dispatch), n=1)
    hint = f" -- did you mean {near[0]}?" if near else ""
    print(f"unknown {PROG} command: {command!r}{hint}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
