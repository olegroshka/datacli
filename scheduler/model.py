"""Infrastructure-neutral scheduler records and JSON contracts."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA_VERSION = 1
COMMAND_CONTRACT_VERSION = "scheduler-commands-v1"
JOB_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,62}$")
WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}


class ContractError(ValueError):
    """A persisted or requested scheduler record violates DD-001."""


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    return f"{stamp}-{uuid.uuid4().hex[:12]}"


def local_timezone_name() -> str:
    index = 1 if time.localtime().tm_isdst > 0 else 0
    return time.tzname[index] or "system-local"


def canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def validate_job_id(value: str) -> str:
    if not JOB_ID_RE.fullmatch(value) or value.casefold() in WINDOWS_RESERVED:
        raise ContractError(
            "job_id must be a lowercase ASCII slug beginning with a letter, "
            "using only letters, digits and hyphens"
        )
    return value


@dataclass(frozen=True)
class ResourceClaim:
    resource_id: str
    mode: str
    scope: str = "user_machine"

    def __post_init__(self) -> None:
        if not self.resource_id:
            raise ContractError("resource_id cannot be empty")
        if self.mode not in {"shared", "exclusive"}:
            raise ContractError(f"unsupported resource mode: {self.mode}")
        if self.scope != "user_machine":
            raise ContractError("first-release resource scope is user_machine")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ResourceClaim":
        return cls(
            resource_id=str(value["resource_id"]),
            mode=str(value["mode"]),
            scope=str(value.get("scope", "user_machine")),
        )


@dataclass(frozen=True)
class RuntimeBinding:
    name: str
    resource_id: str
    resolved_value: str
    source: str
    fingerprint: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.resource_id or not self.resolved_value:
            raise ContractError("runtime bindings require name, resource and value")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeBinding":
        return cls(
            name=str(value["name"]),
            resource_id=str(value["resource_id"]),
            resolved_value=str(value["resolved_value"]),
            source=str(value["source"]),
            fingerprint=(
                str(value["fingerprint"])
                if value.get("fingerprint") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class CommandSpec:
    family: str
    verb: str
    argv: tuple[str, ...]
    contract_version: str
    resources: tuple[ResourceClaim, ...]
    mutation: bool
    network: bool

    def __post_init__(self) -> None:
        if not self.family or not self.verb:
            raise ContractError("command family and verb are required")
        if any("\x00" in arg for arg in self.argv):
            raise ContractError("command argv cannot contain NUL")

    def display(self) -> str:
        import subprocess

        return subprocess.list2cmdline([self.family, self.verb, *self.argv])

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CommandSpec":
        return cls(
            family=str(value["family"]),
            verb=str(value["verb"]),
            argv=tuple(str(x) for x in value.get("argv", [])),
            contract_version=str(value["contract_version"]),
            resources=tuple(
                ResourceClaim.from_dict(x) for x in value.get("resources", [])
            ),
            mutation=bool(value["mutation"]),
            network=bool(value["network"]),
        )


@dataclass(frozen=True)
class TriggerSpec:
    kind: str
    local_time: str | None = None
    days_of_week: tuple[str, ...] = ()
    time_basis: str | None = None
    timezone_at_validation: str | None = None
    start_when_available: bool = True
    wake_to_run: bool = False
    ac_only: bool = True

    def __post_init__(self) -> None:
        if self.kind not in {"daily", "weekly", "manual"}:
            raise ContractError(f"unsupported trigger kind: {self.kind}")
        if self.kind in {"daily", "weekly"}:
            if not self.local_time or not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", self.local_time
            ):
                raise ContractError("calendar trigger time must be HH:MM")
            if self.time_basis != "system_local":
                raise ContractError("calendar triggers use system_local time")
            if not self.timezone_at_validation:
                raise ContractError("calendar triggers record the validation timezone")
        if self.kind == "weekly":
            allowed = {
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            }
            if not self.days_of_week or len(set(self.days_of_week)) != len(
                self.days_of_week
            ):
                raise ContractError("weekly trigger days must be non-empty and unique")
            if set(self.days_of_week) - allowed:
                raise ContractError("weekly trigger contains an invalid day")
        elif self.days_of_week:
            raise ContractError("days_of_week is valid only for weekly triggers")

    @classmethod
    def manual(cls) -> "TriggerSpec":
        return cls(kind="manual", start_when_available=False, ac_only=False)

    @classmethod
    def daily(
        cls, local_time: str, *, wake_to_run: bool = False, ac_only: bool = True
    ) -> "TriggerSpec":
        return cls(
            kind="daily",
            local_time=local_time,
            time_basis="system_local",
            timezone_at_validation=local_timezone_name(),
            wake_to_run=wake_to_run,
            ac_only=ac_only,
        )

    @classmethod
    def weekly(
        cls,
        local_time: str,
        days: Sequence[str],
        *,
        wake_to_run: bool = False,
        ac_only: bool = True,
    ) -> "TriggerSpec":
        return cls(
            kind="weekly",
            local_time=local_time,
            days_of_week=tuple(day.casefold() for day in days),
            time_basis="system_local",
            timezone_at_validation=local_timezone_name(),
            wake_to_run=wake_to_run,
            ac_only=ac_only,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TriggerSpec":
        return cls(
            kind=str(value["kind"]),
            local_time=value.get("local_time"),
            days_of_week=tuple(value.get("days_of_week", [])),
            time_basis=value.get("time_basis"),
            timezone_at_validation=value.get("timezone_at_validation"),
            start_when_available=bool(value.get("start_when_available", True)),
            wake_to_run=bool(value.get("wake_to_run", False)),
            ac_only=bool(value.get("ac_only", True)),
        )


@dataclass(frozen=True)
class ExecutionPolicy:
    on_step_failure: str = "stop"
    same_job_overlap: str = "skip"
    resource_overlap: str = "skip"
    lock_wait_seconds: int = 0
    execution_timeout_seconds: int = 12 * 60 * 60
    retry: str = "none"
    retain_runs: int = 100

    def __post_init__(self) -> None:
        if (
            self.on_step_failure != "stop"
            or self.same_job_overlap != "skip"
            or self.resource_overlap != "skip"
            or self.lock_wait_seconds != 0
            or self.retry != "none"
        ):
            raise ContractError("unsupported first-release execution policy")
        if not (60 <= self.execution_timeout_seconds <= 7 * 24 * 60 * 60):
            raise ContractError("execution timeout must be between 60s and 7 days")
        if self.retain_runs <= 0:
            raise ContractError("retain_runs must be positive")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionPolicy":
        known = {k: value[k] for k in cls.__dataclass_fields__ if k in value}
        return cls(**known)


@dataclass(frozen=True)
class JobSpec:
    profile_id: str
    job_id: str
    generation: int
    display_name: str
    enabled: bool
    repo_root: str
    interpreter: str
    config_path: str | None
    runtime_bindings: tuple[RuntimeBinding, ...]
    command_contract_version: str
    trigger: TriggerSpec
    steps: tuple[CommandSpec, ...]
    policy: ExecutionPolicy
    created_at: str
    updated_at: str
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ContractError(
                f"unsupported schema version {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        validate_job_id(self.job_id)
        try:
            uuid.UUID(self.profile_id)
        except ValueError as exc:
            raise ContractError("profile_id must be a UUID") from exc
        if self.generation <= 0 or not self.display_name.strip() or not self.steps:
            raise ContractError("job generation/display name/steps are required")
        if (
            not Path(self.repo_root).is_absolute()
            or not Path(self.interpreter).is_absolute()
        ):
            raise ContractError("repo_root and interpreter must be absolute paths")
        if self.config_path is not None and not Path(self.config_path).is_absolute():
            raise ContractError("config_path must be absolute when present")
        if not self.runtime_bindings:
            raise ContractError("runtime_bindings cannot be empty")
        if self.command_contract_version != COMMAND_CONTRACT_VERSION:
            raise ContractError("job command contract version is incompatible")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def digest(self) -> str:
        return digest_json(self.to_dict())

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobSpec":
        return cls(
            schema_version=int(value.get("schema_version", 0)),
            profile_id=str(value["profile_id"]),
            job_id=str(value["job_id"]),
            generation=int(value["generation"]),
            display_name=str(value["display_name"]),
            enabled=bool(value["enabled"]),
            repo_root=str(value["repo_root"]),
            interpreter=str(value["interpreter"]),
            config_path=(
                str(value["config_path"])
                if value.get("config_path") is not None
                else None
            ),
            runtime_bindings=tuple(
                RuntimeBinding.from_dict(x) for x in value.get("runtime_bindings", [])
            ),
            command_contract_version=str(value["command_contract_version"]),
            trigger=TriggerSpec.from_dict(value["trigger"]),
            steps=tuple(CommandSpec.from_dict(x) for x in value.get("steps", [])),
            policy=ExecutionPolicy.from_dict(value["policy"]),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )


@dataclass(frozen=True)
class JobDraft:
    draft_id: str
    profile_id: str
    job_id: str
    display_name: str
    trigger: TriggerSpec
    steps: tuple[CommandSpec, ...] = ()
    policy: ExecutionPolicy = field(default_factory=ExecutionPolicy)
    base_generation: int | None = None
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_job_id(self.job_id)
        if not self.draft_id or not self.display_name.strip():
            raise ContractError("draft identity and display name are required")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "JobDraft":
        return cls(
            draft_id=str(value["draft_id"]),
            profile_id=str(value["profile_id"]),
            job_id=str(value["job_id"]),
            display_name=str(value["display_name"]),
            trigger=TriggerSpec.from_dict(value["trigger"]),
            steps=tuple(CommandSpec.from_dict(x) for x in value.get("steps", [])),
            policy=ExecutionPolicy.from_dict(value.get("policy", {})),
            base_generation=(
                int(value["base_generation"])
                if value.get("base_generation") is not None
                else None
            ),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
        )


@dataclass(frozen=True)
class DesiredPointer:
    profile_id: str
    job_id: str
    generation: int
    digest: str | None
    state: str
    updated_at: str

    def __post_init__(self) -> None:
        if self.state not in {"active", "tombstone"}:
            raise ContractError("desired pointer state must be active or tombstone")
        if self.generation <= 0:
            raise ContractError("desired generation must be positive")
        if self.state == "active" and not self.digest:
            raise ContractError("active desired state needs a digest")
        if self.state == "tombstone" and self.digest is not None:
            raise ContractError("tombstone cannot name a digest")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DesiredPointer":
        return cls(
            profile_id=str(value["profile_id"]),
            job_id=str(value["job_id"]),
            generation=int(value["generation"]),
            digest=value.get("digest"),
            state=str(value["state"]),
            updated_at=str(value["updated_at"]),
        )


@dataclass(frozen=True)
class CommandResult:
    outcome: str
    exit_code: int | None
    failure_class: str | None = None
    effect: str = "none"
    retry_guidance: str = "safe"
    summary: str | None = None
    metrics: Mapping[str, Any] = field(default_factory=dict)
    stdout: str = ""
    stderr: str = ""

    def __post_init__(self) -> None:
        if self.outcome not in {
            "succeeded",
            "no_op",
            "failed",
            "cancelled",
            "timed_out",
        }:
            raise ContractError(f"unsupported command outcome: {self.outcome}")
        if self.effect not in {"none", "complete", "partial", "unknown"}:
            raise ContractError(f"unsupported command effect: {self.effect}")
        if self.retry_guidance not in {"safe", "inspect", "unsafe", "unknown"}:
            raise ContractError("unsupported retry guidance")


@dataclass(frozen=True)
class StepResult:
    index: int
    command: str
    started_at: str | None
    finished_at: str | None
    outcome: str
    exit_code: int | None = None
    error_class: str | None = None
    log_offset: str | None = None


@dataclass(frozen=True)
class RunRecord:
    run_id: str
    profile_id: str
    job_id: str
    definition_digest: str
    definition_generation: int
    definition_snapshot_ref: str | None
    snapshot_status: str
    dispatch_kind: str
    backend_cause_hint: str | None
    scheduled_for_hint: str | None
    observed_started_at: str
    finished_at: str
    outcome: str
    steps: tuple[StepResult, ...]
    log_path: str
    journal_path: str
    runtime_binding_fingerprint: str
    runner_version: str
    command_contract_version: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunRecord":
        return cls(
            **{
                **{k: value[k] for k in cls.__dataclass_fields__ if k != "steps"},
                "steps": tuple(StepResult(**x) for x in value.get("steps", [])),
            }
        )


@dataclass(frozen=True)
class DispatchReceipt:
    requested_at: str
    accepted: bool
    backend_token: str | None
    raw_result: str | int | None
    message: str


@dataclass(frozen=True)
class BackendTaskState:
    exists: bool
    enabled: bool | None = None
    state: str = "unknown"
    raw_state: str | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_result_raw: str | int | None = None
    last_result_decoded: str | None = None
    installed_digest: str | None = None
    installed_generation: int | None = None
    missed_run_count: int | None = None
    history_available: bool | None = None
    drift: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReconciliationState:
    desired_digest: str | None
    desired_generation: int | None
    backend: BackendTaskState
    execution: Mapping[str, Any] | None
    state: str
    findings: tuple[str, ...] = ()
