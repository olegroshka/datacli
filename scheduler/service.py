"""Management operations that keep desired, backend, and execution planes separate."""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Sequence

from .backends.base import RunnerAction, SchedulerBackend
from .commands import CommandRegistry, Finding, ValidationContext, default_registry
from .journal import RunJournal
from .model import (
    BackendTaskState,
    DesiredPointer,
    ExecutionPolicy,
    JobDraft,
    JobSpec,
    ReconciliationState,
    TriggerSpec,
    local_timezone_name,
    utc_now,
    validate_job_id,
)
from .runner import JobRunner
from .store import JobStore, NotFound, Profile


class ManagementError(RuntimeError):
    pass


class ActiveRunError(ManagementError):
    pass


class ReadinessError(ManagementError):
    def __init__(self, findings: Sequence[Finding]) -> None:
        self.findings = tuple(findings)
        super().__init__("; ".join(finding.message for finding in findings))


def runner_action(spec: JobSpec, state_root: Path) -> RunnerAction:
    return RunnerAction(
        command=Path(spec.interpreter),
        arguments=(
            "-m",
            "scheduler.runner",
            "--profile-id",
            spec.profile_id,
            "--job-id",
            spec.job_id,
            "--generation",
            str(spec.generation),
            "--digest",
            spec.digest,
            "--dispatch-kind",
            "backend",
            "--state-root",
            str(state_root.resolve()),
        ),
        working_directory=Path(spec.repo_root),
    )


class ScheduleService:
    def __init__(
        self,
        profile: Profile,
        store: JobStore,
        backend: SchedulerBackend,
        *,
        registry: CommandRegistry | None = None,
        validation_context: ValidationContext | None = None,
    ) -> None:
        self.profile = profile
        self.store = store
        self.backend = backend
        self.registry = registry or default_registry()
        self.validation_context = validation_context or ValidationContext.current(
            Path(profile.repo_root),
            Path(profile.interpreter),
            Path(profile.config_path) if profile.config_path else None,
        )
        self.journal = RunJournal(profile, store.state_root)

    def create_draft(
        self,
        job_id: str,
        trigger: TriggerSpec,
        *,
        display_name: str | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> JobDraft:
        validate_job_id(job_id)
        if self.store.get_current(job_id) is not None:
            raise ManagementError(f"job already exists: {job_id}")
        try:
            self.store.get_draft(job_id)
        except NotFound:
            pass
        else:
            raise ManagementError(f"draft already exists: {job_id}")
        return self.store.put_draft(
            JobDraft(
                draft_id=job_id,
                profile_id=self.profile.profile_id,
                job_id=job_id,
                display_name=display_name or job_id,
                trigger=trigger,
                policy=policy or ExecutionPolicy(),
            )
        )

    def add_step(
        self, draft_id: str, family: str, verb: str, argv: Sequence[str]
    ) -> JobDraft:
        draft = self.store.get_draft(draft_id)
        validated = self.registry.validate(family, verb, argv, self.validation_context)
        return self.store.put_draft(
            replace(draft, steps=(*draft.steps, validated.spec), updated_at=utc_now())
        )

    def remove_step(self, draft_id: str, index: int) -> JobDraft:
        draft = self.store.get_draft(draft_id)
        if index < 1 or index > len(draft.steps):
            raise ManagementError(f"step index is out of range: {index}")
        steps = (*draft.steps[: index - 1], *draft.steps[index:])
        return self.store.put_draft(replace(draft, steps=steps, updated_at=utc_now()))

    def replace_step(
        self,
        draft_id: str,
        index: int,
        family: str,
        verb: str,
        argv: Sequence[str],
    ) -> JobDraft:
        draft = self.store.get_draft(draft_id)
        if index < 1 or index > len(draft.steps):
            raise ManagementError(f"step index is out of range: {index}")
        validated = self.registry.validate(family, verb, argv, self.validation_context)
        steps = list(draft.steps)
        steps[index - 1] = validated.spec
        return self.store.put_draft(
            replace(draft, steps=tuple(steps), updated_at=utc_now())
        )

    def begin_edit(self, job_id: str) -> JobDraft:
        base = job_id[:45].rstrip("-")
        draft_id = f"{base}-edit-{uuid.uuid4().hex[:8]}"
        return self.store.clone_to_draft(job_id, draft_id=draft_id)

    def enable_draft(
        self, draft_id: str
    ) -> tuple[DesiredPointer, JobSpec, BackendTaskState]:
        draft = self.store.get_draft(draft_id)
        self._readiness(draft.steps)
        pointer, spec = self.store.commit_draft(
            draft_id, self.registry, self.validation_context, enabled=True
        )
        backend = self.backend.install(spec, runner_action(spec, self.store.state_root))
        return pointer, spec, backend

    def add_one_step(
        self,
        job_id: str,
        trigger: TriggerSpec,
        family: str,
        verb: str,
        argv: Sequence[str],
        *,
        display_name: str | None = None,
        policy: ExecutionPolicy | None = None,
    ) -> tuple[DesiredPointer, JobSpec, BackendTaskState]:
        self.create_draft(job_id, trigger, display_name=display_name, policy=policy)
        try:
            self.add_step(job_id, family, verb, argv)
            return self.enable_draft(job_id)
        except Exception:
            # Preserve a valid draft after backend/desired failures, but never
            # leave an invalid zero-step convenience draft behind.
            try:
                if not self.store.get_draft(job_id).steps:
                    self.store.discard_draft(job_id)
            except Exception:
                pass
            raise

    def _readiness(self, steps: Sequence) -> None:
        bindings = self.registry.resolve_workflow_bindings(
            steps, self.validation_context
        )
        from .commands import ExecutionContext

        context = ExecutionContext(self.validation_context, bindings)
        findings = [
            finding
            for step in steps
            for finding in self.registry.preflight(step, "readiness", context)
        ]
        fatal = [finding for finding in findings if finding.fatal]
        if fatal:
            raise ReadinessError(fatal)

    def set_enabled(
        self, job_id: str, enabled: bool
    ) -> tuple[DesiredPointer, JobSpec, BackendTaskState]:
        self.store.clone_to_draft(job_id)
        if enabled:
            self._readiness(self.store.get_draft(job_id).steps)
        pointer, spec = self.store.commit_draft(
            job_id, self.registry, self.validation_context, enabled=enabled
        )
        backend = self.backend.install(spec, runner_action(spec, self.store.state_root))
        return pointer, spec, backend

    def edit(
        self,
        job_id: str,
        *,
        display_name: str | None = None,
        trigger: TriggerSpec | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[DesiredPointer, JobSpec, BackendTaskState]:
        draft = self.store.clone_to_draft(job_id)
        policy = draft.policy
        if timeout_seconds is not None:
            policy = replace(policy, execution_timeout_seconds=timeout_seconds)
        draft = replace(
            draft,
            display_name=display_name or draft.display_name,
            trigger=trigger or draft.trigger,
            policy=policy,
            updated_at=utc_now(),
        )
        self.store.put_draft(draft)
        self._readiness(draft.steps)
        pointer, spec = self.store.commit_draft(
            job_id,
            self.registry,
            self.validation_context,
            enabled=self.store.get_current_spec(job_id).enabled,
        )
        backend = self.backend.install(spec, runner_action(spec, self.store.state_root))
        return pointer, spec, backend

    def reconcile(self, job_id: str) -> BackendTaskState:
        pointer = self.store.get_current(job_id)
        if pointer is None:
            raise ManagementError(f"unknown job: {job_id}")
        if pointer.state == "tombstone":
            return self.backend.remove(self.profile.profile_id, job_id)
        spec = self.store.get_current_spec(job_id)
        self._readiness(spec.steps)
        return self.backend.install(spec, runner_action(spec, self.store.state_root))

    def delete(self, job_id: str) -> tuple[DesiredPointer, BackendTaskState]:
        active = self.journal.nonterminal_runs(job_id)
        if active:
            raise ActiveRunError(
                f"job has non-terminal run(s): {', '.join(active)}; stop/inspect before delete"
            )
        current = self.store.get_current(job_id)
        if current is None:
            raise ManagementError(f"unknown job: {job_id}")
        pointer = self.store.tombstone(job_id, expected_generation=current.generation)
        backend = self.backend.remove(self.profile.profile_id, job_id)
        return pointer, backend

    def test(self, job_id: str):
        spec = self.store.get_current_spec(job_id)
        return JobRunner(self.store, registry=self.registry).execute(
            job_id,
            spec.generation,
            spec.digest,
            dispatch_kind="foreground_test",
        )

    def status(self, job_id: str) -> ReconciliationState:
        pointer = self.store.get_current(job_id)
        backend = self.backend.query(self.profile.profile_id, job_id)
        records = self.journal.list_records(job_id)
        execution = None
        if records:
            latest = records[0]
            execution = {
                "run_id": latest.run_id,
                "outcome": latest.outcome,
                "started_at": latest.observed_started_at,
                "finished_at": latest.finished_at,
                "definition_generation": latest.definition_generation,
                "definition_digest": latest.definition_digest,
            }
        findings: tuple[str, ...]
        if pointer is None:
            state = "orphan_task" if backend.exists else "unknown"
            findings = ("no desired state exists",)
            desired_digest = None
            desired_generation = None
        elif pointer.state == "tombstone":
            desired_digest = None
            desired_generation = pointer.generation
            if backend.exists:
                state = "delete_pending"
                findings = ("desired state is tombstoned", *backend.drift)
            elif backend.drift:
                state = "unknown"
                findings = backend.drift
            else:
                state = "in_sync"
                findings = ()
        else:
            desired_digest = pointer.digest
            desired_generation = pointer.generation
            findings_list = list(backend.drift)
            spec = self.store.get_current_spec(job_id)
            if not Path(spec.repo_root).is_dir():
                findings_list.append(f"repository is missing: {spec.repo_root}")
            if not Path(spec.interpreter).is_file():
                findings_list.append(f"interpreter is missing: {spec.interpreter}")
            if spec.config_path and not Path(spec.config_path).is_file():
                findings_list.append(f"config is missing: {spec.config_path}")
            if (
                spec.trigger.timezone_at_validation
                and spec.trigger.timezone_at_validation != local_timezone_name()
            ):
                findings_list.append(
                    "system timezone differs from the timezone recorded at validation"
                )
            try:
                observed_bindings = self.registry.resolve_workflow_bindings(
                    spec.steps, self.validation_context
                )
                if observed_bindings != spec.runtime_bindings:
                    findings_list.append(
                        "runtime bindings differ from desired definition"
                    )
            except (ValueError, OSError) as exc:
                findings_list.append(
                    f"runtime binding observation failed: {type(exc).__name__}"
                )
            if not backend.exists:
                state = "unknown" if backend.drift else "missing_task"
            elif (
                backend.installed_digest != pointer.digest
                or backend.installed_generation != pointer.generation
            ):
                state = "stale_task"
                findings_list.append("installed generation/digest differs from desired")
            else:
                if backend.enabled is not None and backend.enabled != spec.enabled:
                    state = "stale_task"
                    findings_list.append("installed enabled state differs from desired")
                elif findings_list:
                    state = "incompatible"
                else:
                    state = "in_sync"
            findings = tuple(findings_list)
        return ReconciliationState(
            desired_digest=desired_digest,
            desired_generation=desired_generation,
            backend=backend,
            execution=execution,
            state=state,
            findings=findings,
        )
