"""Isolated append-only run journals and redacted human logs."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .model import RunRecord, StepResult, canonical_json, utc_now, validate_job_id
from .store import Profile, _secure


class JournalError(RuntimeError):
    pass


class JournalUnavailable(JournalError):
    pass


class JournalCorrupt(JournalError):
    pass


_PATTERNS = (
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(
        r"(?i)((?:api[-_]?key|password|secret|access[-_]?token|refresh[-_]?token)\s*[=:]\s*)[^\s,;]+"
    ),
    re.compile(r"(?i)([?&](?:signature|sig|token|key)=)[^&\s]+"),
)


class Redactor:
    def __init__(self, known_values: Iterable[str] = ()) -> None:
        self.known_values = tuple(
            sorted(
                {value for value in known_values if value and len(value) >= 4},
                key=len,
                reverse=True,
            )
        )

    @classmethod
    def from_environment(cls, environment: Mapping[str, str]) -> "Redactor":
        markers = ("KEY", "TOKEN", "PASSWORD", "SECRET", "CREDENTIAL")
        values = [
            value
            for name, value in environment.items()
            if any(marker in name.upper() for marker in markers)
        ]
        return cls(values)

    def redact(self, value: str) -> str:
        result = value
        for secret in self.known_values:
            result = result.replace(secret, "[REDACTED]")
        for pattern in _PATTERNS:
            result = pattern.sub(r"\1[REDACTED]", result)
        return result


@dataclass
class RunHandle:
    run_id: str
    run_root: Path
    journal_path: Path
    log_path: Path
    redactor: Redactor
    sequence: int = 0
    prior_hash: str | None = None
    max_log_bytes: int = 10 * 1024 * 1024
    log_truncated: bool = False

    def append(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        clean_payload = _redact_value(payload, self.redactor)
        event: dict[str, Any] = {
            "sequence": self.sequence + 1,
            "observed_at": utc_now(),
            "event": event_type,
            "payload": clean_payload,
            "previous_hash": self.prior_hash,
        }
        event["event_hash"] = hashlib.sha256(canonical_json(event)).hexdigest()
        line = json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n"
        try:
            with self.journal_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise JournalUnavailable(f"cannot append run journal: {exc}") from exc
        self.sequence += 1
        self.prior_hash = event["event_hash"]
        return event

    def log(self, value: str) -> str:
        clean = self.redactor.redact(value)
        if not clean:
            return ""
        encoded = clean.encode("utf-8", errors="replace")
        try:
            current = self.log_path.stat().st_size if self.log_path.exists() else 0
            remaining = max(0, self.max_log_bytes - current)
            if remaining <= 0:
                self.log_truncated = True
                return ""
            written = encoded[:remaining]
            with self.log_path.open("ab") as stream:
                stream.write(written)
                if len(written) < len(encoded):
                    marker = b"\n[datacli log truncated]\n"
                    stream.write(
                        marker[: max(0, self.max_log_bytes - current - len(written))]
                    )
                    self.log_truncated = True
                stream.flush()
                os.fsync(stream.fileno())
            return f"{current}:{current + len(written)}"
        except OSError as exc:
            raise JournalUnavailable(f"cannot append run log: {exc}") from exc


def _redact_value(value: Any, redactor: Redactor) -> Any:
    if isinstance(value, str):
        return redactor.redact(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, redactor) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_value(item, redactor) for item in value]
    return value


class RunJournal:
    def __init__(self, profile: Profile, state_root: Path) -> None:
        self.profile = profile
        self.state_root = state_root.resolve()
        self.root = self.state_root / "profiles" / profile.profile_id / "runs"

    def start(
        self,
        job_id: str,
        run_id: str,
        payload: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
    ) -> RunHandle:
        validate_job_id(job_id)
        run_root = self.root / job_id / run_id
        try:
            run_root.mkdir(parents=True, exist_ok=False)
            _secure(run_root, directory=True)
            journal_path = run_root / "events.jsonl"
            log_path = run_root / "run.log"
            journal_path.touch(exist_ok=False)
            log_path.touch(exist_ok=False)
            _secure(journal_path)
            _secure(log_path)
        except OSError as exc:
            raise JournalUnavailable(
                f"cannot establish durable run journal: {exc}"
            ) from exc
        handle = RunHandle(
            run_id=run_id,
            run_root=run_root,
            journal_path=journal_path,
            log_path=log_path,
            redactor=Redactor.from_environment(environment),
        )
        try:
            handle.append("run_received", payload)
        except Exception:
            with contextlib.suppress(OSError):
                shutil.rmtree(run_root)
            raise
        return handle

    def events(self, job_id: str, run_id: str) -> tuple[dict[str, Any], ...]:
        path = self.root / validate_job_id(job_id) / run_id / "events.jsonl"
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise JournalError(f"cannot read journal {path}: {exc}") from exc
        result: list[dict[str, Any]] = []
        prior: str | None = None
        for expected_sequence, line in enumerate(lines, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalCorrupt(f"truncated/invalid event in {path}") from exc
            claimed = event.pop("event_hash", None)
            actual = hashlib.sha256(canonical_json(event)).hexdigest()
            event["event_hash"] = claimed
            if (
                event.get("sequence") != expected_sequence
                or event.get("previous_hash") != prior
                or claimed != actual
            ):
                raise JournalCorrupt(f"event chain mismatch in {path}")
            prior = claimed
            result.append(event)
        return tuple(result)

    def load_record(self, job_id: str, run_id: str) -> RunRecord | None:
        for event in reversed(self.events(job_id, run_id)):
            if event.get("event") == "run_terminal":
                return RunRecord.from_dict(event["payload"]["record"])
        return None

    def _resume_handle(
        self, job_id: str, run_id: str, *, environment: Mapping[str, str]
    ) -> RunHandle:
        events = self.events(job_id, run_id)
        if not events:
            raise JournalCorrupt(f"run journal is empty: {run_id}")
        run_root = self.root / validate_job_id(job_id) / run_id
        return RunHandle(
            run_id=run_id,
            run_root=run_root,
            journal_path=run_root / "events.jsonl",
            log_path=run_root / "run.log",
            redactor=Redactor.from_environment(environment),
            sequence=int(events[-1]["sequence"]),
            prior_hash=str(events[-1]["event_hash"]),
        )

    def recover_abandoned(
        self,
        job_id: str,
        store,
        *,
        environment: Mapping[str, str],
        exclude_run_ids: Iterable[str] = (),
        older_than_seconds: float = 60.0,
    ) -> tuple[RunRecord, ...]:
        """Append terminal abandoned records after a caller proves no runner owns the job.

        The job runner calls this only while holding the per-job OS lock.  The
        age guard avoids misclassifying a concurrently starting zero-wait loser
        whose journal was created just before it attempts that lock.
        """

        excluded = set(exclude_run_ids)
        recovered: list[RunRecord] = []
        now = datetime.now(timezone.utc)
        for run_id in self.nonterminal_runs(job_id):
            if run_id in excluded:
                continue
            events = self.events(job_id, run_id)
            received = events[0]
            observed = str(received.get("observed_at", ""))
            try:
                received_at = datetime.fromisoformat(observed.replace("Z", "+00:00"))
            except ValueError:
                continue
            if (now - received_at).total_seconds() < older_than_seconds:
                continue
            payload = received.get("payload", {})
            generation = int(payload.get("expected_generation", 0))
            digest = str(payload.get("expected_digest", ""))
            dispatch_kind = str(payload.get("dispatch_kind", "direct_runner"))
            try:
                spec = store.get_snapshot(job_id, generation, digest)
                snapshot_ref = store.snapshot_ref(spec)
                snapshot_status = "loaded"
                bindings = spec.runtime_bindings
                step_specs = spec.steps
                contract_version = spec.command_contract_version
            except Exception:
                snapshot_ref = None
                snapshot_status = "missing"
                bindings = ()
                step_specs = ()
                contract_version = "unknown"
            starts = {
                int(event["payload"]["index"]): event
                for event in events
                if event.get("event") == "step_started"
            }
            terminals = {
                int(event["payload"]["index"]): event
                for event in events
                if event.get("event") == "step_terminal"
            }
            steps: list[StepResult] = []
            for index, command in enumerate(step_specs, 1):
                terminal = terminals.get(index)
                start = starts.get(index)
                if terminal:
                    terminal_payload = terminal["payload"]
                    steps.append(
                        StepResult(
                            index=index,
                            command=command.display(),
                            started_at=start.get("observed_at") if start else None,
                            finished_at=terminal.get("observed_at"),
                            outcome=str(terminal_payload.get("outcome", "failed")),
                            exit_code=terminal_payload.get("exit_code"),
                            error_class=terminal_payload.get("failure_class"),
                        )
                    )
                elif start:
                    steps.append(
                        StepResult(
                            index=index,
                            command=command.display(),
                            started_at=start.get("observed_at"),
                            finished_at=utc_now(),
                            outcome="failed",
                            error_class="abandoned_effect_unknown",
                        )
                    )
                else:
                    steps.append(
                        StepResult(index, command.display(), None, None, "not_run")
                    )
            from .commands import workflow_binding_fingerprint

            handle = self._resume_handle(job_id, run_id, environment=environment)
            record = RunRecord(
                run_id=run_id,
                profile_id=self.profile.profile_id,
                job_id=job_id,
                definition_digest=digest,
                definition_generation=generation,
                definition_snapshot_ref=snapshot_ref,
                snapshot_status=snapshot_status,
                dispatch_kind=dispatch_kind,
                backend_cause_hint=None,
                scheduled_for_hint=None,
                observed_started_at=observed,
                finished_at=utc_now(),
                outcome="abandoned",
                steps=tuple(steps),
                log_path=str(handle.log_path),
                journal_path=str(handle.journal_path),
                runtime_binding_fingerprint=(
                    workflow_binding_fingerprint(bindings) if bindings else ""
                ),
                runner_version="datacli-scheduler-recovery-1",
                command_contract_version=contract_version,
            )
            handle.append("run_recovered_abandoned", {"automatic_retry": False})
            handle.append("run_terminal", {"record": record.to_dict()})
            recovered.append(record)
        return tuple(recovered)

    def list_records(self, job_id: str) -> tuple[RunRecord, ...]:
        job_root = self.root / validate_job_id(job_id)
        if not job_root.exists():
            return ()
        records: list[RunRecord] = []
        for run_root in sorted(job_root.iterdir(), reverse=True):
            if not run_root.is_dir():
                continue
            try:
                record = self.load_record(job_id, run_root.name)
            except JournalError:
                continue
            if record is not None:
                records.append(record)
        return tuple(records)

    def nonterminal_runs(self, job_id: str) -> tuple[str, ...]:
        job_root = self.root / validate_job_id(job_id)
        if not job_root.exists():
            return ()
        result: list[str] = []
        for run_root in sorted(job_root.iterdir()):
            if run_root.is_dir():
                try:
                    if self.load_record(job_id, run_root.name) is None:
                        result.append(run_root.name)
                except JournalError:
                    result.append(run_root.name)
        return tuple(result)

    def referenced_digests(self, job_id: str) -> tuple[str, ...]:
        return tuple(
            sorted({record.definition_digest for record in self.list_records(job_id)})
        )

    def purge_terminal_runs(self, job_id: str) -> tuple[str, ...]:
        """Explicit purge helper; callers must enforce user confirmation."""
        removed: list[str] = []
        job_root = self.root / validate_job_id(job_id)
        if not job_root.exists():
            return ()
        for record in self.list_records(job_id):
            target = (job_root / record.run_id).resolve()
            if target.parent != job_root.resolve():
                raise JournalError("refusing purge outside the job run root")
            shutil.rmtree(target)
            removed.append(record.run_id)
        return tuple(removed)
