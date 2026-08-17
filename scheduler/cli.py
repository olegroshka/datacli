"""Headless schedule management CLI shared by datacli's cmd2 delegate."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Sequence

from .backends.windows import WindowsTaskSchedulerBackend
from .commands import (
    EODHD_KINDS,
    CommandValidationError,
    ValidationContext,
    default_registry,
)
from .journal import RunJournal
from .model import ExecutionPolicy, JobDraft, TriggerSpec
from .service import ManagementError, ScheduleService
from .store import (
    GenerationConflict,
    JobStore,
    NotFound,
    Profile,
    ProfileRegistry,
    StoreError,
    _atomic_json,
)

SCHEDULE_OPERATIONS = (
    "commands",
    "profile",
    "list",
    "drafts",
    "add",
    "create",
    "step",
    "enable",
    "show",
    "history",
    "logs",
    "test",
    "pause",
    "resume",
    "stop",
    "delete",
    "reconcile",
    "discard",
    "export",
    "status",
    "run",
    "edit",
    "purge",
    "doctor",
)
SCHEDULE_STEP_OPERATIONS = ("add", "remove", "replace")
SCHEDULE_GLOBAL_OPTIONS = ("--repo-root", "--config", "--profile-id", "--json")
SCHEDULE_WEEKDAYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)
SCHEDULE_OPERATION_OPTIONS: dict[str, tuple[str, ...]] = {
    "add": (
        "--display-name",
        "--timeout",
        "--daily",
        "--weekly",
        "--manual",
        "--days",
        "--wake",
        "--battery",
        "--",
    ),
    "create": (
        "--display-name",
        "--timeout",
        "--daily",
        "--weekly",
        "--manual",
        "--days",
        "--wake",
        "--battery",
    ),
    "logs": ("--run-id",),
    "export": ("--output",),
    "run": ("--wait",),
    "edit": (
        "--draft",
        "--display-name",
        "--timeout",
        "--daily",
        "--weekly",
        "--manual",
        "--days",
        "--wake",
        "--battery",
    ),
    "purge": ("--yes",),
}
SCHEDULE_COMMAND_COMPLETIONS: dict[tuple[str, str], tuple[str, ...]] = {
    ("eodhd", "refresh"): (
        "us_common",
        "uk_eu",
        "us_etf",
        "index_ref",
        "uk_eu_etf",
        "uk_eu_index_ref",
        "news",
        "--run",
        "--fast",
        "--with-fundamentals",
        "--no-universe",
        "--keep-going",
        "--full-refresh",
        "--days",
        "--datasets",
        "--to",
        "--limit",
        "--tickers",
    ),
    ("eodhd", "reindex"): (),
    ("eodhd", "status"): (
        "us_common",
        "uk_eu",
        "us_etf",
        "index_ref",
        "uk_eu_etf",
        "uk_eu_index_ref",
        "news",
        "all",
        "--deep",
        "--json",
        "--no-discovery",
        "--as-of",
        "--stale-days",
        "--min-history-days-for-density",
        "--min-recent-252-rows",
        "--min-history-density",
        "--min-recent-volume-window",
        "--max-zero-volume-ratio",
        "--max-flags-per-lane",
        "--no-color",
        "--color",
    ),
    ("eodhd", "qc"): (
        "us_common",
        "uk_eu",
        "us_etf",
        "index_ref",
        "uk_eu_etf",
        "uk_eu_index_ref",
        "news",
        "all",
        "prices",
        "dividends",
        "splits",
        "--all",
        "--json",
        "--deep",
        "--since",
        "--as-of",
        "--stale-days",
        "--no-color",
        "--color",
    ),
    ("macro", "fetch"): ("--run", "--full", "--provider"),
    ("macro", "status"): (),
    ("sync", "push"): ("--run", "--keep-going", "--with-caches"),
    ("sync", "status"): ("--with-caches",),
}
SCHEDULE_COMMAND_HELP: dict[tuple[str, str], tuple[str, str]] = {
    ("eodhd", "refresh"): (
        "eodhd refresh [LANE ...] [OPTIONS] --run",
        "incremental EODHD acquisition; may use paid API quota",
    ),
    ("eodhd", "reindex"): (
        "eodhd reindex",
        "rebuild local derived indexes without network access",
    ),
    ("eodhd", "status"): (
        "eodhd status [LANE] [OPTIONS]",
        "read-only EODHD coverage and freshness status",
    ),
    ("eodhd", "qc"): (
        "eodhd qc [LANE] [DATASET] [OPTIONS]",
        "read-only quality checks over local EODHD data",
    ),
    ("macro", "fetch"): (
        "macro fetch [--provider fred|eodhd|all] [--full] --run",
        "incremental macro acquisition; requires provider credentials",
    ),
    ("macro", "status"): (
        "macro status",
        "read-only local macro dataset status",
    ),
    ("sync", "push"): (
        "sync push [--with-caches] [--keep-going] --run",
        "push-only backup using the configured backend and cached authentication",
    ),
    ("sync", "status"): (
        "sync status [--with-caches]",
        "read-only backup configuration and manifest status",
    ),
}


def _help_formatter(prog: str) -> argparse.HelpFormatter:
    return argparse.RawDescriptionHelpFormatter(prog, max_help_position=32, width=100)


def _operation_parser(
    subparsers: Any,
    name: str,
    *,
    summary: str,
    description: str,
    examples: Sequence[str] = (),
) -> argparse.ArgumentParser:
    epilog = None
    if examples:
        epilog = "examples:\n" + "\n".join(f"  {example}" for example in examples)
    return subparsers.add_parser(
        name,
        help=summary,
        description=description,
        epilog=epilog,
        formatter_class=_help_formatter,
    )


def _trigger_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument(
        "--daily",
        metavar="HH:MM",
        help="run daily at this Windows system-local wall-clock time",
    )
    group.add_argument(
        "--weekly",
        metavar="HH:MM",
        help="run weekly at this Windows system-local wall-clock time",
    )
    group.add_argument(
        "--manual",
        action="store_true",
        help="install no calendar trigger; dispatch only with `schedule run`",
    )
    parser.add_argument(
        "--days",
        metavar="DAY[,DAY...]",
        help="full weekday names for --weekly, for example monday,wednesday",
    )
    parser.add_argument(
        "--wake",
        action="store_true",
        help="allow a calendar trigger to wake the computer (default: disabled)",
    )
    parser.add_argument(
        "--battery",
        action="store_true",
        help="allow starting on battery (active work is never stopped on transition)",
    )


def _management_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datacli schedule",
        description=(
            "Create and operate recurring datacli workflows backed by Windows Task Scheduler.\n\n"
            "Datacli keeps desired definitions, Windows observations, and run history as three\n"
            "independent state planes. Management never performs paid command work. Scheduled\n"
            "tasks run as the current user with InteractiveToken, so that user must remain logged on."
        ),
        epilog=(
            "common workflows:\n"
            "  datacli schedule commands\n"
            "  datacli schedule add morning --daily 06:00 -- eodhd status --no-discovery\n"
            "  datacli schedule create morning --daily 06:00\n"
            "  datacli schedule step add morning -- eodhd refresh --fast --run\n"
            "  datacli schedule enable morning\n"
            "  datacli schedule status morning\n\n"
            "Global options must appear before the operation. Use `<operation> --help` for\n"
            "behavior, safety notes, and examples."
        ),
        formatter_class=_help_formatter,
    )
    parser.add_argument("--state-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path.cwd(),
        help="repository bound to the scheduler profile (default: current directory)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="explicit datacli.toml identity (default: REPO_ROOT/datacli.toml)",
    )
    parser.add_argument(
        "--profile-id",
        help="use an existing generated profile UUID instead of path discovery",
    )
    parser.add_argument(
        "--json", action="store_true", help="emit stable structured JSON output"
    )
    sub = parser.add_subparsers(
        dest="operation", required=True, title="operations", metavar="OPERATION"
    )

    _operation_parser(
        sub,
        "commands",
        summary="List commands admitted for scheduling",
        description=(
            "List the allowlisted canonical commands and whether each can mutate data or use\n"
            "the network. Arbitrary executables and shell strings cannot be scheduled."
        ),
        examples=("datacli schedule commands", "datacli schedule --json commands"),
    )
    _operation_parser(
        sub,
        "profile",
        summary="Show the active generated profile identity",
        description=(
            "Show the profile UUID and its bound repository, interpreter, and config path.\n"
            "Profile identity is generated and is not inferred from a mutable path."
        ),
        examples=("datacli schedule profile",),
    )
    _operation_parser(
        sub,
        "list",
        summary="List desired jobs and retained tombstones",
        description=(
            "List authoritative desired state for the active profile. Tombstones are retained\n"
            "after deletion so stale Windows actions cannot become valid again."
        ),
        examples=("datacli schedule list",),
    )
    _operation_parser(
        sub,
        "drafts",
        summary="List non-executable workflow drafts",
        description=(
            "List incomplete or generation-based edit drafts. Drafts cannot run and are not\n"
            "installed until `schedule enable DRAFT_ID` succeeds."
        ),
        examples=("datacli schedule drafts",),
    )

    add = _operation_parser(
        sub,
        "add",
        summary="Validate and install a one-step job",
        description=(
            "Create a one-step job, run static/readiness validation, commit generation 1, and\n"
            "reconcile it to Windows. Installation performs no paid command work. The command\n"
            "must follow a literal `--` and must be present in `schedule commands`."
        ),
        examples=(
            "datacli schedule add morning-status --daily 06:00 -- eodhd status --no-discovery",
            "datacli schedule add manual-qc --manual -- eodhd qc us_common",
        ),
    )
    add.add_argument("job_id", help="lowercase job slug, beginning with a letter")
    add.add_argument("--display-name", help="human-readable label (default: JOB_ID)")
    add.add_argument(
        "--timeout",
        type=int,
        default=12 * 60 * 60,
        metavar="SECONDS",
        help="runner soft timeout, 60..604800 seconds (default: %(default)s)",
    )
    _trigger_options(add, required=True)

    create = _operation_parser(
        sub,
        "create",
        summary="Start a non-executable multi-step draft",
        description=(
            "Create an empty workflow draft. Add or replace steps with `schedule step`, inspect\n"
            "it with `schedule show`, then atomically validate/commit/install with `schedule enable`."
        ),
        examples=(
            "datacli schedule create morning --daily 06:00",
            "datacli schedule step add morning -- eodhd refresh --fast --run",
            "datacli schedule enable morning",
        ),
    )
    create.add_argument("job_id", help="lowercase job slug, beginning with a letter")
    create.add_argument("--display-name", help="human-readable label (default: JOB_ID)")
    create.add_argument(
        "--timeout",
        type=int,
        default=12 * 60 * 60,
        metavar="SECONDS",
        help="runner soft timeout, 60..604800 seconds (default: %(default)s)",
    )
    _trigger_options(create, required=True)

    step = _operation_parser(
        sub,
        "step",
        summary="Add, remove, or replace draft steps",
        description=(
            "Edit only a non-executable draft. Step indexes are one-based. Command arguments\n"
            "after the literal `--` are validated as an allowlisted canonical command."
        ),
        examples=(
            "datacli schedule step add morning -- eodhd reindex",
            "datacli schedule step replace morning 1 -- eodhd status --no-discovery",
            "datacli schedule step remove morning 2",
        ),
    )
    step_sub = step.add_subparsers(
        dest="step_operation", required=True, title="step operations", metavar="ACTION"
    )
    step_add = _operation_parser(
        step_sub,
        "add",
        summary="Append one allowlisted command",
        description="Append a validated command to the end of a non-executable draft.",
        examples=("datacli schedule step add morning -- eodhd refresh --fast --run",),
    )
    step_add.add_argument(
        "draft_id", help="draft identifier shown by `schedule drafts`"
    )
    step_remove = _operation_parser(
        step_sub,
        "remove",
        summary="Remove one indexed step",
        description="Remove a one-based step index from a non-executable draft.",
        examples=("datacli schedule step remove morning 2",),
    )
    step_remove.add_argument(
        "draft_id", help="draft identifier shown by `schedule drafts`"
    )
    step_remove.add_argument("index", type=int, help="one-based step index")
    step_replace = _operation_parser(
        step_sub,
        "replace",
        summary="Replace one indexed step",
        description="Replace a one-based draft step with one validated allowlisted command.",
        examples=(
            "datacli schedule step replace morning 1 -- eodhd status --no-discovery",
        ),
    )
    step_replace.add_argument(
        "draft_id", help="draft identifier shown by `schedule drafts`"
    )
    step_replace.add_argument("index", type=int, help="one-based step index")

    operation_docs = {
        "enable": (
            "Validate, commit, and install a draft",
            "Finalise a non-empty draft in one desired-state commit, then install the exact generation in Windows. If backend installation fails, committed desired state remains visible for explicit reconciliation.",
            ("datacli schedule enable morning",),
        ),
        "show": (
            "Show one exact desired definition or draft",
            "Show the active immutable JobSpec for JOB_OR_DRAFT, falling back to a draft with that identifier. Definitions contain no credentials or opaque shell strings.",
            ("datacli schedule show morning",),
        ),
        "history": (
            "Show durable datacli run records",
            "List reconstructed runner records for JOB_ID. This is datacli execution history, not Windows trigger history.",
            ("datacli schedule history morning",),
        ),
        "logs": (
            "Show one redacted runner log",
            "Print the selected run log. Without --run-id, print the newest retained run. Logs are presentation; typed RunRecord outcomes remain authoritative.",
            (
                "datacli schedule logs morning",
                "datacli schedule logs morning --run-id RUN_ID",
            ),
        ),
        "test": (
            "Execute immediately in the foreground",
            "Run the stored exact generation through the shared runner in the current terminal. Unlike `schedule run`, this returns the actual RunRecord and does not ask Windows to dispatch.",
            ("datacli schedule test morning",),
        ),
        "pause": (
            "Disable future Windows dispatches",
            "Commit a disabled generation and reconcile it to Windows. Pausing does not cancel an already active run; use `schedule stop` separately.",
            ("datacli schedule pause morning",),
        ),
        "resume": (
            "Enable future Windows dispatches",
            "Revalidate readiness, commit an enabled generation, and reconcile it to Windows. This does not run the job immediately.",
            ("datacli schedule resume morning",),
        ),
        "stop": (
            "Request cancellation of the Windows task",
            "Ask Task Scheduler to end the active task. Acceptance is not proof that every descendant stopped; confirmation remains unknown unless independently established.",
            ("datacli schedule stop morning",),
        ),
        "delete": (
            "Tombstone desired state and remove the task",
            "Refuse deletion while datacli has a non-terminal run, then write a new tombstone generation and remove the Windows task. Run history and referenced snapshots are retained; use `purge --yes` separately to remove terminal history.",
            ("datacli schedule delete morning",),
        ),
        "reconcile": (
            "Explicitly repair desired/backend drift",
            "Install the current desired generation, or remove the Windows task for a tombstone. Status and doctor never perform this repair implicitly.",
            ("datacli schedule reconcile morning",),
        ),
        "discard": (
            "Discard a non-executable draft",
            "Remove only DRAFT_ID. Desired jobs, installed tasks, snapshots, and run history are unchanged.",
            ("datacli schedule discard morning",),
        ),
        "export": (
            "Export a non-secret desired definition",
            "Write or print the current immutable JobSpec in export format. Exports contain command arguments and paths but never credentials by contract.",
            ("datacli schedule export morning --output morning.json",),
        ),
    }
    for name, (summary, description, examples) in operation_docs.items():
        item = _operation_parser(
            sub,
            name,
            summary=summary,
            description=description,
            examples=examples,
        )
        noun = "draft identifier" if name in {"enable", "discard"} else "job identifier"
        item.add_argument("job_id", help=noun)
        if name == "logs":
            item.add_argument(
                "--run-id", help="exact retained run identifier (default: newest run)"
            )
        if name == "export":
            item.add_argument(
                "--output",
                type=Path,
                metavar="PATH",
                help="write atomically to PATH instead of standard output",
            )

    status = _operation_parser(
        sub,
        "status",
        summary="Read all three state planes without repair",
        description=(
            "Report desired state, locale-neutral Windows observation, and latest datacli run\n"
            "independently. Missing evidence stays unknown. This command never installs, edits,\n"
            "runs, or repairs a task. With no JOB_ID, report every desired job/tombstone."
        ),
        examples=("datacli schedule status", "datacli schedule status morning"),
    )
    status.add_argument("job_id", nargs="?", help="job identifier (default: all jobs)")

    run = _operation_parser(
        sub,
        "run",
        summary="Ask Windows to dispatch an installed job",
        description=(
            "Request an immediate Task Scheduler dispatch. A successful receipt proves only that\n"
            "Windows accepted the request. --wait polls for one unambiguous new datacli RunRecord;\n"
            "a correlation timeout is not reported as job failure."
        ),
        examples=(
            "datacli schedule run morning",
            "datacli schedule run morning --wait",
            "datacli schedule run morning --wait 60",
        ),
    )
    run.add_argument("job_id", help="installed active job identifier")
    run.add_argument(
        "--wait",
        nargs="?",
        const=30.0,
        type=float,
        metavar="SECONDS",
        help="wait for a correlated new run (implicit value: 30 seconds)",
    )

    edit = _operation_parser(
        sub,
        "edit",
        summary="Create and reconcile one new job generation",
        description=(
            "Change display, timeout, trigger, or power settings immediately, or use --draft to\n"
            "start an atomic multi-step edit. A draft records its base generation and cannot\n"
            "overwrite a newer concurrent generation."
        ),
        examples=(
            "datacli schedule edit morning --daily 07:00",
            "datacli schedule edit morning --timeout 7200",
            "datacli schedule edit morning --draft",
        ),
    )
    edit.add_argument("job_id", help="active job identifier")
    edit.add_argument(
        "--draft",
        action="store_true",
        help="Create a generation-based edit draft for step changes",
    )
    edit.add_argument("--display-name", help="new human-readable label")
    edit.add_argument(
        "--timeout",
        type=int,
        metavar="SECONDS",
        help="new runner soft timeout, 60..604800 seconds",
    )
    _trigger_options(edit, required=False)

    purge = _operation_parser(
        sub,
        "purge",
        summary="Irreversibly remove retained terminal history",
        description=(
            "Delete retained terminal run directories for a tombstoned job and then remove only\n"
            "unreferenced snapshots. This is irreversible, requires prior `schedule delete`, and\n"
            "requires --yes. It never removes an active job or Windows task."
        ),
        examples=("datacli schedule purge retired-job --yes",),
    )
    purge.add_argument("job_id", help="tombstoned job identifier")
    purge.add_argument(
        "--yes", action="store_true", help="confirm irreversible history removal"
    )

    doctor = _operation_parser(
        sub,
        "doctor",
        summary="Run read-only runtime and drift diagnostics",
        description=(
            "Check profile paths, interpreter/config availability, backend observations, and\n"
            "reconciliation findings without repairing anything. With no JOB_ID, inspect all jobs."
        ),
        examples=("datacli schedule doctor", "datacli schedule doctor morning"),
    )
    doctor.add_argument("job_id", nargs="?", help="job identifier (default: all jobs)")
    return parser


def _profile_completion_ids(state_root: Path | None) -> tuple[str, ...]:
    try:
        return tuple(
            profile.profile_id for profile in ProfileRegistry(state_root).list()
        )
    except (OSError, StoreError, ValueError):
        return ()


def _completion_inventory(
    *,
    repo_root: Path,
    config_path: Path | None,
    state_root: Path | None,
    profile_id: str | None,
) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "active": (),
        "all_jobs": (),
        "tombstones": (),
        "drafts": (),
        "step_counts": {},
    }
    try:
        profiles = ProfileRegistry(state_root)
        profile = (
            profiles.get(profile_id)
            if profile_id
            else profiles.find(
                repo_root.resolve(), config_path.resolve() if config_path else None
            )
        )
        if profile is None:
            return empty
        store = JobStore(profile, profiles.state_root)
        active: list[str] = []
        all_jobs: list[str] = []
        tombstones: list[str] = []
        for item in store.list(include_tombstones=True):
            all_jobs.append(item.job_id)
            if getattr(item, "state", None) == "tombstone":
                tombstones.append(item.job_id)
            else:
                active.append(item.job_id)
        drafts = store.list_drafts()
        return {
            "active": tuple(active),
            "all_jobs": tuple(all_jobs),
            "tombstones": tuple(tombstones),
            "drafts": tuple(draft.draft_id for draft in drafts),
            "step_counts": {draft.draft_id: len(draft.steps) for draft in drafts},
        }
    except (OSError, StoreError, ValueError):
        return empty


def _argument_after(words: Sequence[str], option: str) -> str | None:
    try:
        index = len(words) - 1 - list(reversed(words)).index(option)
    except ValueError:
        return None
    if index + 1 >= len(words):
        return None
    value = words[index + 1]
    return value if value and not value.startswith("-") else None


def _command_completion_candidates(words: Sequence[str]) -> tuple[str, ...]:
    capabilities = default_registry().list_capabilities()
    families = tuple(sorted({capability.family for capability in capabilities}))
    if not words or not words[0] or words[0] not in families:
        return families
    family = words[0]
    verbs = tuple(
        capability.verb for capability in capabilities if capability.family == family
    )
    if len(words) == 1 or not words[1] or words[1] not in verbs:
        return verbs
    verb = words[1]
    previous = words[-2] if len(words) >= 2 else None
    if previous == "--provider":
        return ("fred", "eodhd", "all")
    if previous == "--datasets":
        return tuple(sorted({*EODHD_KINDS}))
    return SCHEDULE_COMMAND_COMPLETIONS.get((family, verb), ())


def schedule_completion_candidates(
    words: Sequence[str],
    *,
    repo_root: Path | None = None,
    config_path: Path | None = None,
    state_root: Path | None = None,
) -> tuple[str, ...]:
    """Return context-aware cmd2 candidates for arguments after ``schedule``.

    ``words`` includes the token currently being completed. Completion is
    deliberately read-only and returns an empty dynamic inventory when profile
    state is unavailable or corrupt.
    """

    tokens = list(words) or [""]
    previous = tokens[-2] if len(tokens) >= 2 else None
    if previous == "--profile-id":
        return _profile_completion_ids(state_root)

    operation_index = next(
        (index for index, token in enumerate(tokens) if token in SCHEDULE_OPERATIONS),
        None,
    )
    if operation_index is None:
        return ("--help", *SCHEDULE_GLOBAL_OPTIONS, *SCHEDULE_OPERATIONS)

    operation = tokens[operation_index]
    operation_words = tokens[operation_index + 1 :]
    previous = operation_words[-2] if len(operation_words) >= 2 else None

    if previous == "--days":
        return SCHEDULE_WEEKDAYS
    if previous in {"--daily", "--weekly"}:
        return ("06:00", "09:00", "18:00")
    if previous == "--timeout":
        return ("3600", "7200", "43200")
    if previous == "--wait":
        return ("30", "60", "120")

    profile_id = _argument_after(tokens[: operation_index + 1], "--profile-id")
    completed_globals = tokens[: operation_index + 1]
    selected_repo = _argument_after(completed_globals, "--repo-root")
    selected_config = _argument_after(completed_globals, "--config")
    repo = (
        Path(selected_repo).expanduser() if selected_repo else repo_root or Path.cwd()
    )
    config = (
        Path(selected_config).expanduser()
        if selected_config
        else config_path or repo / "datacli.toml"
    )
    inventory = _completion_inventory(
        repo_root=repo,
        config_path=config,
        state_root=state_root,
        profile_id=profile_id,
    )
    options = ("--help", *SCHEDULE_OPERATION_OPTIONS.get(operation, ()))

    if operation == "step":
        if not operation_words or operation_words[0] not in SCHEDULE_STEP_OPERATIONS:
            return ("--help", *SCHEDULE_STEP_OPERATIONS)
        action = operation_words[0]
        action_words = operation_words[1:]
        if "--" in action_words:
            separator = action_words.index("--")
            return _command_completion_candidates(action_words[separator + 1 :])
        if not action_words or len(action_words) == 1:
            return ("--help", *inventory["drafts"])
        if action in {"remove", "replace"} and len(action_words) == 2:
            count = inventory["step_counts"].get(action_words[0], 0)
            return tuple(str(index) for index in range(1, count + 1))
        if action in {"add", "replace"}:
            return ("--help", "--")
        return ("--help",)

    if operation == "add" and "--" in operation_words:
        separator = operation_words.index("--")
        return _command_completion_candidates(operation_words[separator + 1 :])

    if operation in {"add", "create"}:
        return options
    if operation in {"enable", "discard"}:
        return (*options, *inventory["drafts"])
    if operation == "show":
        return (*options, *inventory["all_jobs"], *inventory["drafts"])
    if operation == "purge":
        return (*options, *inventory["tombstones"])
    if operation in {"status", "history", "logs", "delete", "doctor"}:
        return (*options, *inventory["all_jobs"])
    if operation in {"commands", "profile", "list", "drafts"}:
        return options
    return (*options, *inventory["active"])


def _trigger(
    args: argparse.Namespace, *, current: TriggerSpec | None = None
) -> TriggerSpec:
    if args.daily:
        if args.days:
            raise ManagementError("--days is valid only with --weekly")
        return TriggerSpec.daily(
            args.daily, wake_to_run=args.wake, ac_only=not args.battery
        )
    if args.weekly:
        days = [
            item.strip().casefold()
            for item in (args.days or "").split(",")
            if item.strip()
        ]
        if not days:
            raise ManagementError("--weekly requires --days")
        return TriggerSpec.weekly(
            args.weekly, days, wake_to_run=args.wake, ac_only=not args.battery
        )
    if args.manual:
        if args.days:
            raise ManagementError("--days is valid only with --weekly")
        return TriggerSpec.manual()
    if current is not None:
        if args.days or args.wake or args.battery:
            raise ManagementError("power/day options require a new trigger")
        return current
    raise ManagementError("a trigger is required")


def _command(value: Sequence[str]) -> tuple[str, str, list[str]]:
    parts = list(value)
    if parts and parts[0] == "--":
        parts.pop(0)
    if len(parts) < 2:
        raise ManagementError("expected `-- <family> <verb> [arguments...]`")
    return parts[0], parts[1], parts[2:]


def _render(value: Any, *, json_output: bool) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif hasattr(value, "__dataclass_fields__"):
        value = asdict(value)
    if json_output:
        print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list, tuple)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False, default=str)}")
            else:
                print(f"{key}: {item}")
    elif isinstance(value, (list, tuple)):
        for item in value:
            _render(item, json_output=False)
            print()
    else:
        print(value)


def _context(args: argparse.Namespace, *, create_profile: bool):
    state_root = args.state_root.resolve() if args.state_root else None
    profiles = ProfileRegistry(state_root)
    repo = args.repo_root.resolve()
    config = (args.config or repo / "datacli.toml").resolve()
    interpreter = Path(sys.executable).resolve()
    profile: Profile | None
    if args.profile_id:
        profile = profiles.get(args.profile_id)
    else:
        profile = profiles.find(repo, config)
        if profile is None and create_profile:
            profile = profiles.ensure(repo, interpreter, config, label=repo.name)
        if profile is None:
            raise NotFound(
                "no scheduler profile for this repository; `schedule create` or `schedule add` creates one"
            )
    assert profile is not None
    store = JobStore(profile, profiles.state_root)
    backend = WindowsTaskSchedulerBackend(profiles.state_root)
    validation = ValidationContext.current(
        Path(profile.repo_root),
        Path(profile.interpreter),
        Path(profile.config_path) if profile.config_path else None,
    )
    service = ScheduleService(profile, store, backend, validation_context=validation)
    return profiles, profile, store, service


def main(
    argv: Sequence[str] | None = None,
    *,
    backend_factory=None,
) -> int:
    parser = _management_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    command_tail: list[str] | None = None
    if "--" in raw:
        separator = raw.index("--")
        command_tail = raw[separator + 1 :]
        raw = raw[:separator]
    args = parser.parse_args(raw)
    if args.operation == "add" or (
        args.operation == "step" and args.step_operation in {"add", "replace"}
    ):
        if command_tail is None:
            parser.error("command steps must follow a literal `--` separator")
        args.command = command_tail
    elif command_tail is not None:
        parser.error("the `--` command separator is valid only for add/step add")
    if args.operation == "commands":
        _render(
            [
                {
                    "command": capability.identity,
                    "usage": SCHEDULE_COMMAND_HELP[
                        (capability.family, capability.verb)
                    ][0],
                    "summary": SCHEDULE_COMMAND_HELP[
                        (capability.family, capability.verb)
                    ][1],
                    "class": capability.classification,
                    "mutation": capability.mutation,
                    "network": capability.network,
                }
                for capability in default_registry().list_capabilities()
            ],
            json_output=args.json,
        )
        return 0
    mutating_profile = args.operation in {"add", "create"}
    try:
        profiles, profile, store, service = _context(
            args, create_profile=mutating_profile
        )
        if backend_factory is not None:
            service.backend = backend_factory(profiles.state_root)
        operation = args.operation
        if operation == "profile":
            _render(profile, json_output=args.json)
        elif operation == "list":
            _render(store.list(include_tombstones=True), json_output=args.json)
        elif operation == "drafts":
            _render(store.list_drafts(), json_output=args.json)
        elif operation == "create":
            draft = service.create_draft(
                args.job_id,
                _trigger(args),
                display_name=args.display_name,
                policy=ExecutionPolicy(execution_timeout_seconds=args.timeout),
            )
            _render(draft, json_output=args.json)
        elif operation == "add":
            family, verb, command_argv = _command(args.command)
            pointer, spec, backend = service.add_one_step(
                args.job_id,
                _trigger(args),
                family,
                verb,
                command_argv,
                display_name=args.display_name,
                policy=ExecutionPolicy(execution_timeout_seconds=args.timeout),
            )
            _render(
                {"desired": pointer.to_dict(), "backend": asdict(backend)},
                json_output=args.json,
            )
        elif operation == "step":
            if args.step_operation == "remove":
                value = service.remove_step(args.draft_id, args.index)
            else:
                family, verb, command_argv = _command(args.command)
                if args.step_operation == "add":
                    value = service.add_step(args.draft_id, family, verb, command_argv)
                else:
                    value = service.replace_step(
                        args.draft_id, args.index, family, verb, command_argv
                    )
            _render(value, json_output=args.json)
        elif operation == "enable":
            pointer, spec, backend = service.enable_draft(args.job_id)
            _render(
                {"desired": pointer.to_dict(), "backend": asdict(backend)},
                json_output=args.json,
            )
        elif operation == "show":
            try:
                value = store.get_current_spec(args.job_id)
            except NotFound:
                value = store.get_draft(args.job_id)
            _render(value, json_output=args.json)
        elif operation == "status":
            jobs = (
                [args.job_id]
                if args.job_id
                else [item.job_id for item in store.list(include_tombstones=True)]
            )
            _render(
                [asdict(service.status(job_id)) for job_id in jobs],
                json_output=args.json,
            )
        elif operation == "history":
            _render(
                RunJournal(profile, store.state_root).list_records(args.job_id),
                json_output=args.json,
            )
        elif operation == "logs":
            journal = RunJournal(profile, store.state_root)
            records = journal.list_records(args.job_id)
            record = next(
                (record for record in records if record.run_id == args.run_id),
                records[0] if records and not args.run_id else None,
            )
            if record is None:
                raise NotFound("run log not found")
            print(Path(record.log_path).read_text(encoding="utf-8"))
        elif operation == "test":
            _render(service.test(args.job_id), json_output=args.json)
        elif operation == "run":
            journal = RunJournal(profile, store.state_root)
            before = {record.run_id for record in journal.list_records(args.job_id)}
            receipt = service.backend.run_now(profile.profile_id, args.job_id)
            result: dict[str, Any] = {"dispatch": asdict(receipt)}
            if args.wait is not None and receipt.accepted:
                deadline = time.monotonic() + max(0, args.wait)
                matched = []
                while time.monotonic() < deadline:
                    matched = [
                        record
                        for record in journal.list_records(args.job_id)
                        if record.run_id not in before
                    ]
                    if len(matched) == 1:
                        break
                    time.sleep(0.2)
                result["run"] = asdict(matched[0]) if len(matched) == 1 else None
                result["correlation"] = (
                    "matched" if len(matched) == 1 else "timeout_or_ambiguous"
                )
            _render(result, json_output=args.json)
            return 0 if receipt.accepted else 1
        elif operation in {"pause", "resume"}:
            pointer, spec, backend = service.set_enabled(
                args.job_id, operation == "resume"
            )
            _render(
                {"desired": pointer.to_dict(), "backend": asdict(backend)},
                json_output=args.json,
            )
        elif operation == "stop":
            _render(
                service.backend.stop(profile.profile_id, args.job_id),
                json_output=args.json,
            )
        elif operation == "edit":
            if args.draft:
                if any(
                    (
                        args.display_name,
                        args.timeout,
                        args.daily,
                        args.weekly,
                        args.manual,
                        args.days,
                        args.wake,
                        args.battery,
                    )
                ):
                    raise ManagementError(
                        "--draft cannot be combined with immediate edit options"
                    )
                _render(service.begin_edit(args.job_id), json_output=args.json)
                return 0
            current = store.get_current_spec(args.job_id)
            trigger = _trigger(args, current=current.trigger)
            pointer, spec, backend = service.edit(
                args.job_id,
                display_name=args.display_name,
                trigger=trigger,
                timeout_seconds=args.timeout,
            )
            _render(
                {"desired": pointer.to_dict(), "backend": asdict(backend)},
                json_output=args.json,
            )
        elif operation == "delete":
            pointer, backend = service.delete(args.job_id)
            _render(
                {"desired": pointer.to_dict(), "backend": asdict(backend)},
                json_output=args.json,
            )
        elif operation == "reconcile":
            _render(service.reconcile(args.job_id), json_output=args.json)
        elif operation == "discard":
            store.discard_draft(args.job_id)
            _render({"discarded": args.job_id}, json_output=args.json)
        elif operation == "purge":
            pointer = store.get_current(args.job_id)
            if not args.yes:
                raise ManagementError(
                    "purge requires --yes and reports irreversible history loss"
                )
            if pointer is None or pointer.state != "tombstone":
                raise ManagementError("purge requires a deleted/tombstoned job")
            journal = RunJournal(profile, store.state_root)
            removed_runs = journal.purge_terminal_runs(args.job_id)
            removed_snapshots = store.purge_unreferenced_snapshots(args.job_id, ())
            _render(
                {"removed_runs": removed_runs, "removed_snapshots": removed_snapshots},
                json_output=args.json,
            )
        elif operation == "doctor":
            jobs = (
                [args.job_id]
                if args.job_id
                else [item.job_id for item in store.list(include_tombstones=True)]
            )
            report = {
                "profile": asdict(profile),
                "repo_exists": Path(profile.repo_root).is_dir(),
                "interpreter_exists": Path(profile.interpreter).is_file(),
                "config_exists": bool(
                    profile.config_path and Path(profile.config_path).is_file()
                ),
                "jobs": [asdict(service.status(job_id)) for job_id in jobs],
                "repair_performed": False,
            }
            _render(report, json_output=args.json)
        elif operation == "export":
            spec = store.get_current_spec(args.job_id)
            payload = {"export_version": 1, "job": spec.to_dict()}
            if args.output:
                _atomic_json(args.output.resolve(), payload)
                _render({"exported": str(args.output.resolve())}, json_output=args.json)
            else:
                print(json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=False))
        else:  # pragma: no cover
            parser.error(f"unimplemented operation {operation}")
        return 0
    except (
        CommandValidationError,
        GenerationConflict,
        ManagementError,
        NotFound,
        StoreError,
        ValueError,
    ) as exc:
        print(f"schedule: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"schedule backend: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
