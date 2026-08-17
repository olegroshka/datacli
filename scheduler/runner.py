"""Infrastructure-neutral, journal-first workflow runner."""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import replace
from pathlib import Path
from typing import Mapping, Sequence

from .commands import (
    CommandRegistry,
    CommandValidationError,
    ExecutionContext,
    ValidationContext,
    default_registry,
    workflow_binding_fingerprint,
)
from .journal import JournalUnavailable, RunHandle, RunJournal
from .locks import LockManager, LockUnavailable
from .model import (
    COMMAND_CONTRACT_VERSION,
    ResourceClaim,
    RunRecord,
    StepResult,
    new_run_id,
    utc_now,
)
from .store import JobStore, NotFound, ProfileRegistry

RUNNER_VERSION = "datacli-scheduler-1"
EXIT_BY_OUTCOME = {
    "succeeded": 0,
    "no_op": 0,
    "failed": 1,
    "invalid": 2,
    "cancelled": 3,
    "timed_out": 3,
    "environment_unavailable": 69,
    "skipped_overlap": 75,
    "stale_dispatch": 76,
    "journal_unavailable": 78,
}


class JobRunner:
    def __init__(
        self,
        store: JobStore,
        *,
        registry: CommandRegistry | None = None,
        journal: RunJournal | None = None,
        lock_manager: LockManager | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.store = store
        self.registry = registry or default_registry()
        self.environment = dict(os.environ if environment is None else environment)
        self.journal = journal or RunJournal(store.profile, store.state_root)
        self.lock_manager = lock_manager or LockManager(store.state_root / "locks")

    def execute(
        self,
        job_id: str,
        expected_generation: int,
        expected_digest: str,
        *,
        dispatch_kind: str,
        backend_cause_hint: str | None = None,
        scheduled_for_hint: str | None = None,
    ) -> RunRecord:
        run_id = new_run_id()
        observed_started = utc_now()
        handle = self.journal.start(
            job_id,
            run_id,
            {
                "profile_id": self.store.profile.profile_id,
                "job_id": job_id,
                "expected_generation": expected_generation,
                "expected_digest": expected_digest,
                "dispatch_kind": dispatch_kind,
            },
            environment=self.environment,
        )
        try:
            spec = self.store.get_snapshot(job_id, expected_generation, expected_digest)
        except NotFound as exc:
            handle.append("snapshot_missing", {"error": str(exc)})
            return self._terminal(
                handle,
                observed_started,
                job_id,
                expected_generation,
                expected_digest,
                dispatch_kind,
                "invalid",
                (),
                snapshot_status="missing",
                snapshot_ref=None,
                backend_cause_hint=backend_cause_hint,
                scheduled_for_hint=scheduled_for_hint,
            )
        snapshot_ref = self.store.snapshot_ref(spec)
        pointer = self.store.get_current(job_id)
        if (
            pointer is None
            or pointer.state != "active"
            or pointer.generation != expected_generation
            or pointer.digest != expected_digest
        ):
            handle.append(
                "stale_dispatch", {"current": pointer.to_dict() if pointer else None}
            )
            return self._terminal(
                handle,
                observed_started,
                job_id,
                expected_generation,
                expected_digest,
                dispatch_kind,
                "stale_dispatch",
                (),
                snapshot_status="loaded",
                snapshot_ref=snapshot_ref,
                backend_cause_hint=backend_cause_hint,
                scheduled_for_hint=scheduled_for_hint,
            )

        validation = ValidationContext.current(
            repo_root=Path(spec.repo_root),
            interpreter=Path(spec.interpreter),
            config_path=Path(spec.config_path) if spec.config_path else None,
            environment=self.environment,
        )
        try:
            validated_steps = tuple(
                self.registry.validate(
                    step.family, step.verb, step.argv, validation
                ).spec
                for step in spec.steps
            )
            if validated_steps != spec.steps:
                raise CommandValidationError(
                    "derived command contract/resources drifted"
                )
            current_bindings = self.registry.resolve_workflow_bindings(
                spec.steps, validation
            )
            if current_bindings != spec.runtime_bindings:
                raise CommandValidationError(
                    "runtime bindings drifted from the stored definition"
                )
        except (CommandValidationError, ValueError) as exc:
            handle.append("validation_failed", {"error": str(exc)})
            return self._terminal(
                handle,
                observed_started,
                job_id,
                expected_generation,
                expected_digest,
                dispatch_kind,
                "invalid",
                (),
                snapshot_status="loaded",
                snapshot_ref=snapshot_ref,
                backend_cause_hint=backend_cause_hint,
                scheduled_for_hint=scheduled_for_hint,
            )

        claims = [
            ResourceClaim(f"scheduler:job:{spec.profile_id}:{spec.job_id}", "exclusive")
        ]
        claims.extend(claim for step in spec.steps for claim in step.resources)
        owner = {
            "pid": os.getpid(),
            "run_id": run_id,
            "profile_id": spec.profile_id,
            "job_id": spec.job_id,
        }
        try:
            with self.lock_manager.acquire_many(claims, owner=owner, wait_seconds=0):
                pointer = self.store.get_current(job_id)
                if (
                    pointer is None
                    or pointer.state != "active"
                    or pointer.generation != expected_generation
                    or pointer.digest != expected_digest
                ):
                    handle.append(
                        "stale_dispatch_after_lock",
                        {"current": pointer.to_dict() if pointer else None},
                    )
                    return self._terminal(
                        handle,
                        observed_started,
                        job_id,
                        expected_generation,
                        expected_digest,
                        dispatch_kind,
                        "stale_dispatch",
                        (),
                        snapshot_status="loaded",
                        snapshot_ref=snapshot_ref,
                        backend_cause_hint=backend_cause_hint,
                        scheduled_for_hint=scheduled_for_hint,
                    )
                self.journal.recover_abandoned(
                    job_id,
                    self.store,
                    environment=self.environment,
                    exclude_run_ids=(run_id,),
                )
                context = ExecutionContext(
                    validation=validation,
                    bindings=spec.runtime_bindings,
                    noninteractive=True,
                )
                for step in spec.steps:
                    findings = self.registry.preflight(step, "runtime", context)
                    fatal = [finding for finding in findings if finding.fatal]
                    if fatal:
                        handle.append(
                            "runtime_preflight_failed",
                            {"findings": [vars(finding) for finding in findings]},
                        )
                        return self._terminal(
                            handle,
                            observed_started,
                            job_id,
                            expected_generation,
                            expected_digest,
                            dispatch_kind,
                            "environment_unavailable",
                            tuple(self._not_run(spec.steps)),
                            snapshot_status="loaded",
                            snapshot_ref=snapshot_ref,
                            bindings=spec.runtime_bindings,
                            backend_cause_hint=backend_cause_hint,
                            scheduled_for_hint=scheduled_for_hint,
                        )
                return self._run_steps(
                    spec,
                    handle,
                    observed_started,
                    dispatch_kind,
                    validation,
                    backend_cause_hint,
                    scheduled_for_hint,
                )
        except LockUnavailable as exc:
            handle.append(
                "overlap_skipped",
                {"resource_id": exc.resource_id, "holder": exc.holder},
            )
            return self._terminal(
                handle,
                observed_started,
                job_id,
                expected_generation,
                expected_digest,
                dispatch_kind,
                "skipped_overlap",
                tuple(self._not_run(spec.steps)),
                snapshot_status="loaded",
                snapshot_ref=snapshot_ref,
                bindings=spec.runtime_bindings,
                backend_cause_hint=backend_cause_hint,
                scheduled_for_hint=scheduled_for_hint,
            )

    def _run_steps(
        self,
        spec,
        handle: RunHandle,
        observed_started: str,
        dispatch_kind: str,
        validation: ValidationContext,
        backend_cause_hint: str | None,
        scheduled_for_hint: str | None,
    ) -> RunRecord:
        started_monotonic = time.monotonic()
        results: list[StepResult] = []
        terminal_outcome = "succeeded"
        for index, command in enumerate(spec.steps, 1):
            remaining = spec.policy.execution_timeout_seconds - (
                time.monotonic() - started_monotonic
            )
            if remaining <= 0:
                terminal_outcome = "timed_out"
                results.append(
                    StepResult(index, command.display(), None, None, "not_run")
                )
                results.extend(self._not_run(spec.steps[index:], start=index + 1))
                break
            step_started = utc_now()
            handle.append(
                "step_started", {"index": index, "command": command.display()}
            )
            result = self.registry.execute(
                command,
                ExecutionContext(
                    validation=validation,
                    bindings=spec.runtime_bindings,
                    noninteractive=True,
                    timeout_seconds=remaining,
                ),
            )
            log_offset = handle.log(
                f"\n===== step {index}: {command.display()} =====\n"
                f"{result.stdout}{result.stderr}"
            )
            step_finished = utc_now()
            results.append(
                StepResult(
                    index=index,
                    command=command.display(),
                    started_at=step_started,
                    finished_at=step_finished,
                    outcome=result.outcome,
                    exit_code=result.exit_code,
                    error_class=result.failure_class,
                    log_offset=log_offset or None,
                )
            )
            handle.append(
                "step_terminal",
                {
                    "index": index,
                    "outcome": result.outcome,
                    "exit_code": result.exit_code,
                    "effect": result.effect,
                    "retry_guidance": result.retry_guidance,
                    "failure_class": result.failure_class,
                },
            )
            if result.outcome in {"failed", "cancelled", "timed_out"}:
                terminal_outcome = result.outcome
                results.extend(self._not_run(spec.steps[index:], start=index + 1))
                break
        else:
            if results and all(result.outcome == "no_op" for result in results):
                terminal_outcome = "no_op"
        return self._terminal(
            handle,
            observed_started,
            spec.job_id,
            spec.generation,
            spec.digest,
            dispatch_kind,
            terminal_outcome,
            tuple(results),
            snapshot_status="loaded",
            snapshot_ref=self.store.snapshot_ref(spec),
            bindings=spec.runtime_bindings,
            backend_cause_hint=backend_cause_hint,
            scheduled_for_hint=scheduled_for_hint,
        )

    @staticmethod
    def _not_run(commands: Sequence, *, start: int = 1) -> list[StepResult]:
        return [
            StepResult(index, command.display(), None, None, "not_run")
            for index, command in enumerate(commands, start)
        ]

    def _terminal(
        self,
        handle: RunHandle,
        observed_started: str,
        job_id: str,
        generation: int,
        digest: str,
        dispatch_kind: str,
        outcome: str,
        steps: tuple[StepResult, ...],
        *,
        snapshot_status: str,
        snapshot_ref: str | None,
        bindings=(),
        backend_cause_hint: str | None,
        scheduled_for_hint: str | None,
    ) -> RunRecord:
        record = RunRecord(
            run_id=handle.run_id,
            profile_id=self.store.profile.profile_id,
            job_id=job_id,
            definition_digest=digest,
            definition_generation=generation,
            definition_snapshot_ref=snapshot_ref,
            snapshot_status=snapshot_status,
            dispatch_kind=dispatch_kind,
            backend_cause_hint=backend_cause_hint,
            scheduled_for_hint=scheduled_for_hint,
            observed_started_at=observed_started,
            finished_at=utc_now(),
            outcome=outcome,
            steps=steps,
            log_path=str(handle.log_path),
            journal_path=str(handle.journal_path),
            runtime_binding_fingerprint=(
                workflow_binding_fingerprint(bindings) if bindings else ""
            ),
            runner_version=RUNNER_VERSION,
            command_contract_version=COMMAND_CONTRACT_VERSION,
        )
        handle.append("run_terminal", {"record": record.to_dict()})
        return record


def exit_code(record: RunRecord) -> int:
    return EXIT_BY_OUTCOME.get(record.outcome, 1)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Internal datacli scheduled-job runner"
    )
    parser.add_argument("--profile-id", required=True)
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--generation", type=int, required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument(
        "--dispatch-kind",
        choices=("backend", "foreground_test", "direct_runner"),
        default="backend",
    )
    parser.add_argument("--state-root", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state_root = args.state_root.resolve() if args.state_root else None
    profiles = ProfileRegistry(state_root)
    profile = profiles.get(args.profile_id)
    store = JobStore(profile, profiles.state_root)
    try:
        record = JobRunner(store).execute(
            args.job_id,
            args.generation,
            args.digest,
            dispatch_kind=args.dispatch_kind,
        )
    except JournalUnavailable as exc:
        print(f"scheduler runner: {exc}", file=sys.stderr)
        return 78
    print(f"{record.run_id} {record.outcome}")
    return exit_code(record)


if __name__ == "__main__":
    sys.exit(main())
