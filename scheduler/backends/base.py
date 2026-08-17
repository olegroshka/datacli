"""Infrastructure-neutral scheduler backend port and safe fake."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..model import BackendTaskState, DispatchReceipt, JobSpec, utc_now


@dataclass(frozen=True)
class RunnerAction:
    command: Path
    arguments: tuple[str, ...]
    working_directory: Path


@dataclass(frozen=True)
class CancellationReceipt:
    requested_at: str
    accepted: bool
    confirmed_terminal: bool | None
    raw_result: str | int | None
    message: str


class SchedulerBackend(Protocol):
    def install(self, spec: JobSpec, action: RunnerAction) -> BackendTaskState: ...

    def remove(self, profile_id: str, job_id: str) -> BackendTaskState: ...

    def enable(self, profile_id: str, job_id: str) -> BackendTaskState: ...

    def disable(self, profile_id: str, job_id: str) -> BackendTaskState: ...

    def run_now(self, profile_id: str, job_id: str) -> DispatchReceipt: ...

    def stop(self, profile_id: str, job_id: str) -> CancellationReceipt: ...

    def query(self, profile_id: str, job_id: str) -> BackendTaskState: ...


class FakeBackend:
    """In-memory contract fake; it never starts a process or touches the OS."""

    def __init__(self) -> None:
        self.tasks: dict[tuple[str, str], tuple[JobSpec, RunnerAction]] = {}
        self.fail_install = False
        self.dispatch_accepted = True

    def install(self, spec: JobSpec, action: RunnerAction) -> BackendTaskState:
        if self.fail_install:
            raise RuntimeError("injected install failure")
        self.tasks[(spec.profile_id, spec.job_id)] = (spec, action)
        return self.query(spec.profile_id, spec.job_id)

    def remove(self, profile_id: str, job_id: str) -> BackendTaskState:
        self.tasks.pop((profile_id, job_id), None)
        return self.query(profile_id, job_id)

    def enable(self, profile_id: str, job_id: str) -> BackendTaskState:
        spec, action = self.tasks[(profile_id, job_id)]
        from dataclasses import replace

        self.tasks[(profile_id, job_id)] = (replace(spec, enabled=True), action)
        return self.query(profile_id, job_id)

    def disable(self, profile_id: str, job_id: str) -> BackendTaskState:
        spec, action = self.tasks[(profile_id, job_id)]
        from dataclasses import replace

        self.tasks[(profile_id, job_id)] = (replace(spec, enabled=False), action)
        return self.query(profile_id, job_id)

    def run_now(self, profile_id: str, job_id: str) -> DispatchReceipt:
        exists = (profile_id, job_id) in self.tasks
        accepted = exists and self.dispatch_accepted
        return DispatchReceipt(
            requested_at=utc_now(),
            accepted=accepted,
            backend_token=None,
            raw_result=0 if accepted else 1,
            message="dispatch accepted" if accepted else "dispatch rejected",
        )

    def stop(self, profile_id: str, job_id: str) -> CancellationReceipt:
        exists = (profile_id, job_id) in self.tasks
        return CancellationReceipt(
            requested_at=utc_now(),
            accepted=exists,
            confirmed_terminal=None,
            raw_result=0 if exists else 1,
            message=(
                "cancellation request accepted; descendant termination is unverified"
                if exists
                else "task not found"
            ),
        )

    def query(self, profile_id: str, job_id: str) -> BackendTaskState:
        value = self.tasks.get((profile_id, job_id))
        if value is None:
            return BackendTaskState(exists=False, state="unknown")
        spec, _ = value
        return BackendTaskState(
            exists=True,
            enabled=spec.enabled,
            state="ready" if spec.enabled else "disabled",
            installed_digest=spec.digest,
            installed_generation=spec.generation,
            history_available=False,
        )
