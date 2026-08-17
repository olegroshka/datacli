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
from .commands import CommandValidationError, ValidationContext, default_registry
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


def _trigger_options(parser: argparse.ArgumentParser, *, required: bool) -> None:
    group = parser.add_mutually_exclusive_group(required=required)
    group.add_argument("--daily", metavar="HH:MM")
    group.add_argument("--weekly", metavar="HH:MM")
    group.add_argument("--manual", action="store_true")
    parser.add_argument("--days", help="comma-separated days for --weekly")
    parser.add_argument("--wake", action="store_true")
    parser.add_argument(
        "--battery", action="store_true", help="allow starting while on battery"
    )


def _management_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="datacli schedule")
    parser.add_argument("--state-root", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--config", type=Path)
    parser.add_argument("--profile-id")
    parser.add_argument("--json", action="store_true")
    sub = parser.add_subparsers(dest="operation", required=True)

    sub.add_parser("commands", help="List the schedulable command allowlist")
    sub.add_parser("profile", help="Show the active generated profile identity")
    sub.add_parser("list", help="List desired jobs and tombstones")
    sub.add_parser("drafts", help="List non-executable workflow drafts")

    add = sub.add_parser(
        "add",
        help="Validate, commit and install a one-step job",
        epilog="command step: -- <family> <verb> [arguments...]",
    )
    add.add_argument("job_id")
    add.add_argument("--display-name")
    add.add_argument("--timeout", type=int, default=12 * 60 * 60)
    _trigger_options(add, required=True)

    create = sub.add_parser("create", help="Create a non-executable workflow draft")
    create.add_argument("job_id")
    create.add_argument("--display-name")
    create.add_argument("--timeout", type=int, default=12 * 60 * 60)
    _trigger_options(create, required=True)

    step = sub.add_parser("step", help="Edit draft workflow steps")
    step_sub = step.add_subparsers(dest="step_operation", required=True)
    step_add = step_sub.add_parser(
        "add", epilog="command step: -- <family> <verb> [arguments...]"
    )
    step_add.add_argument("draft_id")
    step_remove = step_sub.add_parser("remove")
    step_remove.add_argument("draft_id")
    step_remove.add_argument("index", type=int)
    step_replace = step_sub.add_parser(
        "replace", epilog="replacement step: -- <family> <verb> [arguments...]"
    )
    step_replace.add_argument("draft_id")
    step_replace.add_argument("index", type=int)

    operation_help = {
        "enable": "Finalise a draft and reconcile it to Windows",
        "show": "Show one exact desired definition or draft",
        "history": "Show datacli run records",
        "logs": "Show a redacted run log",
        "test": "Run now in the foreground through the shared runner",
        "pause": "Disable future dispatches without stopping an active run",
        "resume": "Enable future dispatches",
        "stop": "Request cancellation; descendant confirmation may be unknown",
        "delete": "Tombstone desired state and remove the Windows task",
        "reconcile": "Explicitly repair desired/backend drift",
        "discard": "Discard a non-executable draft",
        "export": "Export a non-secret desired definition",
    }
    for name in (
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
    ):
        item = sub.add_parser(name, help=operation_help[name])
        item.add_argument("job_id")
        if name == "logs":
            item.add_argument("--run-id")
        if name == "export":
            item.add_argument("--output", type=Path)

    status = sub.add_parser("status", help="Read all three state planes without repair")
    status.add_argument("job_id", nargs="?")

    run = sub.add_parser(
        "run", help="Ask Windows to dispatch; acceptance is not completion"
    )
    run.add_argument("job_id")
    run.add_argument("--wait", nargs="?", const=30.0, type=float)

    edit = sub.add_parser("edit", help="Commit and reconcile one new generation")
    edit.add_argument("job_id")
    edit.add_argument(
        "--draft",
        action="store_true",
        help="Create a generation-based edit draft for step changes",
    )
    edit.add_argument("--display-name")
    edit.add_argument("--timeout", type=int)
    _trigger_options(edit, required=False)

    purge = sub.add_parser(
        "purge", help="Irreversibly remove retained terminal history"
    )
    purge.add_argument("job_id")
    purge.add_argument("--yes", action="store_true")

    doctor = sub.add_parser("doctor", help="Read-only runtime and drift diagnostics")
    doctor.add_argument("job_id", nargs="?")
    return parser


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
