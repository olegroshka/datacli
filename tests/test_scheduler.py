from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import replace
from pathlib import Path

import pytest

from scheduler.backends.base import FakeBackend, RunnerAction
from scheduler.backends.windows import (
    OBSERVATION_SCRIPT,
    TASK_NS,
    WindowsTaskSchedulerBackend,
    build_task_xml,
)
from scheduler.cli import (
    SCHEDULE_COMMAND_COMPLETIONS,
    SCHEDULE_COMMAND_HELP,
    SCHEDULE_OPERATION_OPTIONS,
    SCHEDULE_OPERATIONS,
    _management_parser,
)
from scheduler.cli import main as schedule_main
from scheduler.cli import (
    schedule_completion_candidates,
)
from scheduler.commands import (
    CommandValidationError,
    ExecutionContext,
    ValidationContext,
    default_registry,
    direct_mutation_lock,
)
from scheduler.journal import JournalUnavailable, RunJournal
from scheduler.locks import LockManager, LockUnavailable
from scheduler.model import (
    BackendTaskState,
    CommandResult,
    ExecutionPolicy,
    JobDraft,
    ResourceClaim,
    TriggerSpec,
)
from scheduler.runner import JobRunner
from scheduler.service import ScheduleService, runner_action
from scheduler.store import GenerationConflict, JobStore, ProfileRegistry, StoreError

REPO = Path(__file__).resolve().parents[1]


def _config(tmp_path: Path, *, local_sync: bool = False) -> tuple[Path, Path]:
    data = tmp_path / "data"
    data.mkdir()
    lines = ["[eodhd]", f'data_root = "{data.as_posix()}"']
    if local_sync:
        target = tmp_path / "backup"
        lines += [
            "",
            "[sync]",
            'backend = "local"',
            f'local_dest = "{target.as_posix()}"',
        ]
    path = tmp_path / "datacli.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path, data


def _store(tmp_path: Path, *, job_id: str = "morning", steps: int = 1):
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profiles = ProfileRegistry(state)
    profile = profiles.ensure(REPO, Path(sys.executable), config, label="test")
    store = JobStore(profile, state)
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    command = registry.validate("eodhd", "reindex", [], context).spec
    draft = JobDraft(
        draft_id=job_id,
        profile_id=profile.profile_id,
        job_id=job_id,
        display_name="Morning",
        trigger=TriggerSpec.manual(),
        steps=tuple(command for _ in range(steps)),
    )
    store.put_draft(draft)
    pointer, spec = store.commit_draft(job_id, registry, context)
    return profiles, profile, store, context, pointer, spec


def test_registry_matches_admitted_inventory_and_rejects_forbidden(
    tmp_path: Path,
) -> None:
    config, _ = _config(tmp_path, local_sync=True)
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    assert {cap.identity for cap in registry.list_capabilities()} == {
        "eodhd refresh",
        "eodhd reindex",
        "eodhd status",
        "eodhd qc",
        "macro fetch",
        "macro status",
        "sync push",
        "sync status",
    }
    with pytest.raises(CommandValidationError, match="requires its own --run"):
        registry.validate("eodhd", "refresh", ["--fast"], context)
    with pytest.raises(CommandValidationError, match="not admitted"):
        registry.validate("sync", "login", [], context)
    with pytest.raises(CommandValidationError, match="secrets"):
        registry.validate("eodhd", "refresh", ["--run", "api_key=canary"], context)
    with pytest.raises(CommandValidationError, match="unsupported option"):
        registry.validate("eodhd", "status", ["--write"], context)
    command = registry.validate("eodhd", "refresh", ["--fast", "--run"], context)
    assert command.spec.mutation and command.spec.network
    assert any(claim.mode == "exclusive" for claim in command.spec.resources)


def test_registry_rejects_sync_containment(tmp_path: Path) -> None:
    data = tmp_path / "data"
    data.mkdir()
    config = tmp_path / "datacli.toml"
    config.write_text(
        f'[eodhd]\ndata_root = "{data.as_posix()}"\n\n'
        f'[sync]\nbackend = "local"\nlocal_dest = "{(data / "backup").as_posix()}"\n',
        encoding="utf-8",
    )
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    with pytest.raises(CommandValidationError, match="must not contain"):
        default_registry().validate("sync", "push", ["--run"], context)


def test_shared_and_exclusive_os_locks(tmp_path: Path) -> None:
    manager = LockManager(tmp_path / "locks")
    shared = ResourceClaim("path:c:/fixture", "shared")
    exclusive = ResourceClaim("path:c:/fixture", "exclusive")
    first = manager.acquire(shared)
    try:
        second = manager.acquire(shared)
        second.release()
        with pytest.raises(LockUnavailable):
            manager.acquire(exclusive)
    finally:
        first.release()
    acquired = manager.acquire(exclusive)
    acquired.release()


def test_direct_and_scheduled_mutations_contend_on_identical_resource(
    tmp_path: Path,
) -> None:
    config, _ = _config(tmp_path)
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    validated = registry.validate("eodhd", "reindex", [], context)
    manager = LockManager(tmp_path / "locks")
    held = manager.acquire_many(validated.spec.resources)
    held.__enter__()
    try:
        with pytest.raises(LockUnavailable):
            with direct_mutation_lock(
                "eodhd", "reindex", [], context=context, lock_manager=manager
            ):
                pass
    finally:
        held.__exit__(None, None, None)


def test_registry_executes_safe_local_sync_and_returns_typed_noop(
    tmp_path: Path,
) -> None:
    config, data = _config(tmp_path, local_sync=True)
    (data / "STATUS.json").write_text("{}", encoding="utf-8")
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    registry = default_registry()
    validated = registry.validate("sync", "push", ["--run"], context)
    execution = ExecutionContext(context, validated.bindings, timeout_seconds=30)
    first = registry.execute(validated.spec, execution)
    second = registry.execute(validated.spec, execution)
    assert first.outcome == "succeeded" and first.effect == "complete"
    assert second.outcome == "no_op" and second.effect == "none"
    assert (tmp_path / "backup" / "STATUS.json").read_text(encoding="utf-8") == "{}"


def test_store_round_trip_cas_tombstone_and_snapshot_retention(tmp_path: Path) -> None:
    profiles, profile, store, context, pointer, first = _store(tmp_path)
    assert (
        profiles.ensure(REPO, Path(sys.executable), context.config_path).profile_id
        == profile.profile_id
    )
    assert store.get_current_spec("morning") == first
    assert store.get_snapshot("morning", 1, pointer.digest).digest == pointer.digest

    store.clone_to_draft("morning")
    second_pointer, second = store.commit_draft(
        "morning", default_registry(), context, enabled=False
    )
    assert second_pointer.generation == 2 and not second.enabled
    assert store.get_snapshot("morning", 1, pointer.digest) == first
    with pytest.raises(GenerationConflict):
        store.commit_validated(first, expected_generation=0)
    tombstone = store.tombstone("morning", expected_generation=2)
    assert tombstone.state == "tombstone" and tombstone.generation == 3
    assert store.get_snapshot("morning", 1, pointer.digest) == first


def test_empty_draft_never_becomes_desired_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    store.put_draft(
        JobDraft(
            draft_id="empty",
            profile_id=profile.profile_id,
            job_id="empty",
            display_name="Empty",
            trigger=TriggerSpec.manual(),
        )
    )
    with pytest.raises(StoreError, match="empty draft"):
        store.commit_draft(
            "empty",
            default_registry(),
            ValidationContext.current(
                REPO, Path(sys.executable), config, environment={}
            ),
        )
    assert store.get_current("empty") is None


def test_schema_rejects_unknown_version_and_snapshot_has_no_secret_fields(
    tmp_path: Path,
) -> None:
    _, _, store, _, pointer, spec = _store(tmp_path)
    value = spec.to_dict()
    value["schema_version"] = 999
    from scheduler.model import ContractError, JobSpec

    with pytest.raises(ContractError, match="unsupported schema"):
        JobSpec.from_dict(value)
    snapshot = Path(store.snapshot_ref(spec)).read_text(encoding="utf-8").casefold()
    assert "api_key" not in snapshot and "password" not in snapshot
    schema = json.loads(
        (REPO / "scheduler" / "schema" / "job-spec-v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["properties"]["schema_version"]["const"] == 1


class _ResultRegistry:
    def __init__(self, results: list[CommandResult]) -> None:
        self.base = default_registry()
        self.results = results
        self.calls = 0

    def validate(self, *args, **kwargs):
        return self.base.validate(*args, **kwargs)

    def resolve_workflow_bindings(self, *args, **kwargs):
        return self.base.resolve_workflow_bindings(*args, **kwargs)

    def preflight(self, *args, **kwargs):
        return ()

    def execute(self, *args, **kwargs):
        result = self.results[self.calls]
        self.calls += 1
        return result


def test_runner_stops_on_failure_redacts_and_preserves_typed_effect(
    tmp_path: Path,
) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path, steps=2)
    secret = "scheduler-canary-secret"
    registry = _ResultRegistry(
        [
            CommandResult(
                "failed",
                9,
                failure_class="fixture_failure",
                effect="partial",
                retry_guidance="inspect",
                stdout=f"api_key={secret}\n",
            ),
            CommandResult("succeeded", 0, effect="complete"),
        ]
    )
    record = JobRunner(
        store, registry=registry, environment={"EODHD_API_KEY": secret}
    ).execute("morning", 1, pointer.digest, dispatch_kind="foreground_test")
    assert record.outcome == "failed"
    assert [step.outcome for step in record.steps] == ["failed", "not_run"]
    assert registry.calls == 1
    log = Path(record.log_path).read_text(encoding="utf-8")
    assert secret not in log and "[REDACTED]" in log
    events = RunJournal(profile, store.state_root).events("morning", record.run_id)
    assert events[-1]["event"] == "run_terminal"
    assert events[-2]["payload"]["effect"] == "partial"


def test_runner_stale_dispatch_and_overlap_do_not_execute(tmp_path: Path) -> None:
    _, profile, store, context, old_pointer, _ = _store(tmp_path)
    store.clone_to_draft("morning")
    store.commit_draft("morning", default_registry(), context)
    stale_registry = _ResultRegistry([CommandResult("succeeded", 0)])
    stale = JobRunner(store, registry=stale_registry).execute(
        "morning", 1, old_pointer.digest, dispatch_kind="backend"
    )
    assert stale.outcome == "stale_dispatch" and stale_registry.calls == 0

    current = store.get_current("morning")
    assert current and current.digest
    manager = LockManager(store.state_root / "locks")
    claim = ResourceClaim(f"scheduler:job:{profile.profile_id}:morning", "exclusive")
    held = manager.acquire(claim, owner={"run_id": "holder"})
    try:
        overlap_registry = _ResultRegistry([CommandResult("succeeded", 0)])
        overlap = JobRunner(
            store, registry=overlap_registry, lock_manager=manager
        ).execute(
            "morning", current.generation, current.digest, dispatch_kind="backend"
        )
    finally:
        held.release()
    assert overlap.outcome == "skipped_overlap" and overlap_registry.calls == 0


def test_runner_binding_drift_fails_closed(tmp_path: Path) -> None:
    _, _, store, context, pointer, _ = _store(tmp_path)
    assert context.config_path
    Path(context.config_path).write_text(
        f'[eodhd]\ndata_root = "{(tmp_path / "other").as_posix()}"\n',
        encoding="utf-8",
    )
    registry = _ResultRegistry([CommandResult("succeeded", 0)])
    record = JobRunner(store, registry=registry, environment={}).execute(
        "morning", 1, pointer.digest, dispatch_kind="backend"
    )
    assert record.outcome == "invalid" and registry.calls == 0


def test_concurrent_runner_starts_yield_execution_and_durable_skip(
    tmp_path: Path,
) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path)
    started = threading.Event()
    release = threading.Event()

    class BlockingRegistry(_ResultRegistry):
        def execute(self, *args, **kwargs):
            self.calls += 1
            started.set()
            assert release.wait(5)
            return CommandResult("succeeded", 0, effect="complete")

    winner_registry = BlockingRegistry([])
    holder: dict[str, object] = {}

    def first_run():
        holder["record"] = JobRunner(store, registry=winner_registry).execute(
            "morning", 1, pointer.digest, dispatch_kind="backend"
        )

    thread = threading.Thread(target=first_run)
    thread.start()
    assert started.wait(5)
    loser_registry = _ResultRegistry([CommandResult("succeeded", 0)])
    loser = JobRunner(store, registry=loser_registry).execute(
        "morning", 1, pointer.digest, dispatch_kind="backend"
    )
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    assert holder["record"].outcome == "succeeded"  # type: ignore[union-attr]
    assert loser.outcome == "skipped_overlap" and loser_registry.calls == 0
    outcomes = {
        record.outcome
        for record in RunJournal(profile, store.state_root).list_records("morning")
    }
    assert {"succeeded", "skipped_overlap"} <= outcomes


def test_journal_establishment_failure_prevents_execution(tmp_path: Path) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path)
    registry = _ResultRegistry([CommandResult("succeeded", 0)])

    class BrokenJournal(RunJournal):
        def start(self, *args, **kwargs):
            raise JournalUnavailable("disk full fixture")

    runner = JobRunner(
        store,
        registry=registry,
        journal=BrokenJournal(profile, store.state_root),
    )
    with pytest.raises(JournalUnavailable, match="disk full"):
        runner.execute("morning", 1, pointer.digest, dispatch_kind="backend")
    assert registry.calls == 0


def test_crash_recovery_marks_abandoned_without_retry(tmp_path: Path) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path)
    journal = RunJournal(profile, store.state_root)
    handle = journal.start(
        "morning",
        "abandoned-fixture",
        {
            "profile_id": profile.profile_id,
            "job_id": "morning",
            "expected_generation": 1,
            "expected_digest": pointer.digest,
            "dispatch_kind": "backend",
        },
        environment={},
    )
    handle.append("step_started", {"index": 1, "command": "eodhd reindex"})
    recovered = journal.recover_abandoned(
        "morning", store, environment={}, older_than_seconds=0
    )
    assert len(recovered) == 1 and recovered[0].outcome == "abandoned"
    assert recovered[0].steps[0].error_class == "abandoned_effect_unknown"
    events = journal.events("morning", "abandoned-fixture")
    assert any(
        event["event"] == "run_recovered_abandoned"
        and event["payload"]["automatic_retry"] is False
        for event in events
    )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job Object oracle")
def test_soft_timeout_controls_nested_process_tree(tmp_path: Path) -> None:
    child = tmp_path / "child.py"
    child.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    parent = tmp_path / "parent.py"
    parent.write_text(
        "import subprocess,sys,time\n"
        "subprocess.Popen([sys.executable, sys.argv[1]])\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    from scheduler.commands import _run_process_tree

    with pytest.raises(subprocess.TimeoutExpired) as raised:
        _run_process_tree(
            [sys.executable, str(parent), str(child)],
            cwd=tmp_path,
            env={},
            timeout=0.2,
        )
    assert raised.value.tree_controlled is True  # type: ignore[attr-defined]


def test_windows_xml_encodes_accepted_runtime_policy_without_secrets(
    tmp_path: Path,
) -> None:
    _, _, store, _, _, base = _store(tmp_path)
    spec = replace(base, trigger=TriggerSpec.daily("06:00"))
    action = runner_action(spec, store.state_root)
    payload = build_task_xml(spec, action, user_id="TEST\\user")
    text = payload.decode("utf-16")
    assert "scheduler.runner" in text
    assert "EODHD_API_KEY" not in text and "password" not in text.casefold()
    root = ET.fromstring(text)
    ns = {"t": TASK_NS}
    assert (
        root.findtext("t:Settings/t:MultipleInstancesPolicy", namespaces=ns)
        == "Parallel"
    )
    assert (
        root.findtext("t:Settings/t:DisallowStartIfOnBatteries", namespaces=ns)
        == "true"
    )
    assert (
        root.findtext("t:Settings/t:StopIfGoingOnBatteries", namespaces=ns) == "false"
    )
    assert (
        root.findtext("t:Settings/t:RunOnlyIfNetworkAvailable", namespaces=ns)
        == "false"
    )
    assert (
        root.findtext("t:Principals/t:Principal/t:LogonType", namespaces=ns)
        == "InteractiveToken"
    )
    assert len(root.findall("t:Actions/t:Exec", ns)) == 1


def test_windows_adapter_uses_argument_arrays_and_removes_temp_xml(
    tmp_path: Path,
) -> None:
    _, _, store, _, _, spec = _store(tmp_path)
    captured: dict[str, object] = {}

    def invoke(args):
        assert isinstance(args, list)
        if "/Create" in args:
            xml_path = Path(args[args.index("/XML") + 1])
            captured["xml"] = xml_path.read_bytes().decode("utf-16")
            captured["create"] = list(args)
            return subprocess.CompletedProcess(args, 0, "", "")
        if "/Query" in args:
            return subprocess.CompletedProcess(args, 0, captured["xml"], "")
        raise AssertionError(args)

    def observe(_name):
        return subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                {
                    "State": "Ready",
                    "Enabled": True,
                    "NextRunTime": "2026-08-18T06:00:00.0000000+01:00",
                    "LastRunTime": None,
                    "LastTaskResult": 0,
                    "NumberOfMissedRuns": 0,
                    "HistoryAvailable": False,
                }
            ),
            "",
        )

    backend = WindowsTaskSchedulerBackend(
        store.state_root, invoker=invoke, observer=observe, user_id="TEST\\user"
    )
    state = backend.install(spec, runner_action(spec, store.state_root))
    assert state.exists and state.installed_digest == spec.digest
    assert state.state == "ready" and state.history_available is False
    assert state.last_result_decoded and "succeeded" in state.last_result_decoded
    assert not list((store.state_root / "backend-temp").glob("*.xml"))
    assert captured["create"][0] == "schtasks.exe"  # type: ignore[index]


def test_windows_observer_failure_stays_unknown(tmp_path: Path) -> None:
    _, _, store, _, _, spec = _store(tmp_path)
    action = runner_action(spec, store.state_root)
    xml = build_task_xml(spec, action, user_id="TEST\\user").decode("utf-16")

    def invoke(args):
        return subprocess.CompletedProcess(args, 0, xml, "")

    def unavailable(_name):
        raise FileNotFoundError("PowerShell fixture unavailable")

    backend = WindowsTaskSchedulerBackend(
        store.state_root,
        invoker=invoke,
        observer=unavailable,
        user_id="TEST\\user",
    )
    state = backend.query(spec.profile_id, spec.job_id)
    assert state.exists and state.state == "unknown"
    assert state.history_available is None
    assert any("runtime_observation_unavailable" in item for item in state.drift)


def test_windows_observer_proves_clean_absence_after_xml_query_failure(
    tmp_path: Path,
) -> None:
    _, _, store, _, _, spec = _store(tmp_path)

    def missing(args):
        return subprocess.CompletedProcess(args, 1, "", "not found")

    def absent(_name):
        return subprocess.CompletedProcess(
            ["powershell.exe"], 0, json.dumps({"Exists": False}), ""
        )

    backend = WindowsTaskSchedulerBackend(
        store.state_root,
        invoker=missing,
        observer=absent,
        user_id="TEST\\user",
    )
    state = backend.query(spec.profile_id, spec.job_id)
    assert not state.exists and state.state == "missing"
    assert not state.drift
    assert "Exists = $false" in OBSERVATION_SCRIPT


def test_windows_never_run_result_suppresses_placeholder_timestamp(
    tmp_path: Path,
) -> None:
    _, _, store, _, _, spec = _store(tmp_path)
    action = runner_action(spec, store.state_root)
    xml = build_task_xml(spec, action, user_id="TEST\\user").decode("utf-16")

    def invoke(args):
        return subprocess.CompletedProcess(args, 0, xml, "")

    def never_run(_name):
        return subprocess.CompletedProcess(
            ["powershell.exe"],
            0,
            json.dumps(
                {
                    "State": "Ready",
                    "Enabled": True,
                    "NextRunTime": None,
                    "LastRunTime": None,
                    "LastTaskResult": 267011,
                    "NumberOfMissedRuns": 0,
                    "HistoryAvailable": False,
                }
            ),
            "",
        )

    backend = WindowsTaskSchedulerBackend(
        store.state_root,
        invoker=invoke,
        observer=never_run,
        user_id="TEST\\user",
    )
    state = backend.query(spec.profile_id, spec.job_id)
    assert state.last_run_at is None
    assert state.last_result_raw == 267011
    assert state.last_result_decoded and "not yet run" in state.last_result_decoded
    assert "$result -ne 267011" in OBSERVATION_SCRIPT


def test_windows_observer_is_fixed_non_shell_probe(monkeypatch) -> None:
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "{}", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    name = "Datacli-12345678-fixture"
    WindowsTaskSchedulerBackend._observe_powershell(name)
    assert captured["shell"] is False
    assert captured["env"]["DATACLI_TASK_NAME"] == name
    assert name not in captured["args"]
    assert "Get-ScheduledTask" in captured["args"][-1]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows token identity")
def test_windows_principal_uses_effective_token_identity(monkeypatch) -> None:
    def fake_run(args, **kwargs):
        assert args == ["whoami.exe"]
        assert kwargs["shell"] is False
        return subprocess.CompletedProcess(args, 0, "DOMAIN\\effective-user\n", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert WindowsTaskSchedulerBackend._current_user() == "DOMAIN\\effective-user"


def test_windows_registration_failure_includes_backend_diagnostic(
    tmp_path: Path,
) -> None:
    _, _, store, _, _, spec = _store(tmp_path)

    def reject(args):
        return subprocess.CompletedProcess(args, 1, "", "invalid principal\n")

    backend = WindowsTaskSchedulerBackend(
        store.state_root, invoker=reject, user_id="TEST\\user"
    )
    with pytest.raises(RuntimeError, match="invalid principal"):
        backend.install(spec, runner_action(spec, store.state_root))


def test_service_lifecycle_and_three_plane_status(tmp_path: Path) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    backend = FakeBackend()
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    service = ScheduleService(profile, store, backend, validation_context=context)
    service.create_draft("daily", TriggerSpec.daily("06:00"))
    service.add_step("daily", "eodhd", "reindex", [])
    pointer, spec, _ = service.enable_draft("daily")
    status = service.status("daily")
    assert status.state == "in_sync"
    assert status.desired_generation == 1 and status.execution is None
    paused, paused_spec, _ = service.set_enabled("daily", False)
    assert paused.generation == 2 and not paused_spec.enabled
    deleted, backend_state = service.delete("daily")
    assert deleted.state == "tombstone" and not backend_state.exists
    assert store.get_snapshot("daily", 1, pointer.digest) == spec


def test_tombstone_status_stays_unknown_when_absence_is_unproven(
    tmp_path: Path,
) -> None:
    class AmbiguousMissingBackend(FakeBackend):
        def query(self, profile_id: str, job_id: str) -> BackendTaskState:
            return BackendTaskState(
                exists=False,
                state="unknown",
                drift=("query_failed_exit_1",),
            )

    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    service = ScheduleService(
        profile,
        store,
        AmbiguousMissingBackend(),
        validation_context=ValidationContext.current(
            REPO, Path(sys.executable), config, environment={}
        ),
    )
    service.create_draft("ambiguous-delete", TriggerSpec.manual())
    service.add_step("ambiguous-delete", "eodhd", "reindex", [])
    pointer, _, _ = service.enable_draft("ambiguous-delete")
    store.tombstone("ambiguous-delete", expected_generation=pointer.generation)
    status = service.status("ambiguous-delete")
    assert status.state == "unknown"
    assert status.findings == ("query_failed_exit_1",)


def test_status_reports_runtime_binding_and_timezone_drift(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    backend = FakeBackend()
    context = ValidationContext.current(
        REPO, Path(sys.executable), config, environment={}
    )
    service = ScheduleService(profile, store, backend, validation_context=context)
    service.create_draft("drift", TriggerSpec.daily("06:00"))
    service.add_step("drift", "eodhd", "reindex", [])
    service.enable_draft("drift")
    config.write_text(
        f'[eodhd]\ndata_root = "{(tmp_path / "moved").as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr("scheduler.service.local_timezone_name", lambda: "Other Zone")
    status = service.status("drift")
    assert status.state == "incompatible"
    assert any("runtime bindings differ" in item for item in status.findings)
    assert any("timezone differs" in item for item in status.findings)


def test_multi_step_edit_draft_applies_once_and_stale_base_conflicts(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    backend = FakeBackend()
    service = ScheduleService(
        profile,
        store,
        backend,
        validation_context=ValidationContext.current(
            REPO, Path(sys.executable), config, environment={}
        ),
    )
    service.create_draft("editable", TriggerSpec.manual())
    service.add_step("editable", "eodhd", "reindex", [])
    service.enable_draft("editable")

    applied = service.begin_edit("editable")
    service.replace_step(applied.draft_id, 1, "eodhd", "status", [])
    service.add_step(applied.draft_id, "eodhd", "reindex", [])
    _, edited, _ = service.enable_draft(applied.draft_id)
    assert edited.generation == 2
    assert [(step.family, step.verb) for step in edited.steps] == [
        ("eodhd", "status"),
        ("eodhd", "reindex"),
    ]

    stale = service.begin_edit("editable")
    service.edit("editable", display_name="newer")
    with pytest.raises(GenerationConflict, match="based on generation"):
        service.enable_draft(stale.draft_id)


def test_backend_install_failure_keeps_desired_state_for_reconciliation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    profile = ProfileRegistry(state).ensure(REPO, Path(sys.executable), config)
    store = JobStore(profile, state)
    backend = FakeBackend()
    backend.fail_install = True
    service = ScheduleService(
        profile,
        store,
        backend,
        validation_context=ValidationContext.current(
            REPO, Path(sys.executable), config, environment={}
        ),
    )
    service.create_draft("degraded", TriggerSpec.manual())
    service.add_step("degraded", "eodhd", "reindex", [])
    with pytest.raises(RuntimeError, match="injected"):
        service.enable_draft("degraded")
    assert store.get_current_spec("degraded").generation == 1
    assert service.status("degraded").state == "missing_task"


def test_documented_cli_draft_grammar_with_fake_backend(tmp_path: Path, capsys) -> None:
    state = tmp_path / "state"
    config, _ = _config(tmp_path)
    fake = FakeBackend()

    def factory(_state_root):
        return fake

    common = [
        "--state-root",
        str(state),
        "--repo-root",
        str(REPO),
        "--config",
        str(config),
    ]
    assert (
        schedule_main(
            [*common, "create", "morning", "--daily", "06:00"],
            backend_factory=factory,
        )
        == 0
    )
    assert (
        schedule_main(
            [*common, "step", "add", "morning", "--", "eodhd", "reindex"],
            backend_factory=factory,
        )
        == 0
    )
    assert schedule_main([*common, "enable", "morning"], backend_factory=factory) == 0
    assert (
        schedule_main([*common, "--json", "status", "morning"], backend_factory=factory)
        == 0
    )
    output = capsys.readouterr().out
    assert '"state": "in_sync"' in output


def test_scheduler_help_explains_workflows_safety_and_defaults(capsys) -> None:
    with pytest.raises(SystemExit) as top_exit:
        schedule_main(["--help"])
    assert top_exit.value.code == 0
    top = capsys.readouterr().out
    assert "three\nindependent state planes" in top
    assert "Global options must appear before the operation" in top
    assert "datacli schedule step add morning -- eodhd refresh --fast --run" in top

    with pytest.raises(SystemExit) as add_exit:
        schedule_main(["add", "--help"])
    assert add_exit.value.code == 0
    add = capsys.readouterr().out
    assert "literal `--`" in add
    assert "60..604800 seconds" in add
    assert "Windows system-local wall-clock time" in add
    assert "Installation performs no paid command work" in add

    with pytest.raises(SystemExit) as run_exit:
        schedule_main(["run", "--help"])
    assert run_exit.value.code == 0
    run = capsys.readouterr().out
    assert "accepted the request" in run
    assert "implicit value: 30 seconds" in run

    with pytest.raises(SystemExit) as purge_exit:
        schedule_main(["purge", "--help"])
    assert purge_exit.value.code == 0
    purge = capsys.readouterr().out
    assert "irreversible" in purge
    assert "requires --yes" in purge

    assert schedule_main(["commands"]) == 0
    commands = capsys.readouterr().out
    assert "eodhd refresh [LANE ...] [OPTIONS] --run" in commands
    assert "may use paid API quota" in commands
    assert "push-only backup" in commands


def test_every_scheduler_parser_surface_has_descriptive_help() -> None:
    parser = _management_parser()

    def inspect(current: argparse.ArgumentParser) -> None:
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                for child in action.choices.values():
                    assert child.description
                    inspect(child)
                continue
            if action.dest == "help" or action.help is argparse.SUPPRESS:
                continue
            assert action.help, f"missing help for {current.prog}: {action.dest}"

    inspect(parser)
    operations = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    assert tuple(operations.choices) == SCHEDULE_OPERATIONS
    for name, child in operations.choices.items():
        parser_options = {
            option
            for action in child._actions
            for option in action.option_strings
            if option not in {"-h", "--help"}
        }
        completed_options = set(SCHEDULE_OPERATION_OPTIONS.get(name, ())) - {"--"}
        assert parser_options == completed_options


def test_scheduler_completion_covers_registry_and_command_values() -> None:
    registry_keys = {
        (capability.family, capability.verb)
        for capability in default_registry().list_capabilities()
    }
    assert set(SCHEDULE_COMMAND_COMPLETIONS) == registry_keys
    assert set(SCHEDULE_COMMAND_HELP) == registry_keys
    assert set(SCHEDULE_OPERATIONS) <= set(schedule_completion_candidates([""]))
    assert schedule_completion_candidates(["step", ""]) == (
        "--help",
        "add",
        "remove",
        "replace",
    )
    assert set(schedule_completion_candidates(["add", "demo", "--", ""])) == {
        "eodhd",
        "macro",
        "sync",
    }
    assert set(schedule_completion_candidates(["add", "demo", "--", "eodhd", "r"])) == {
        "refresh",
        "reindex",
        "status",
        "qc",
    }
    assert schedule_completion_candidates(
        ["add", "demo", "--", "macro", "fetch", "--provider", ""]
    ) == ("fred", "eodhd", "all")
    assert schedule_completion_candidates(["create", "demo", "--days", ""]) == (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )


def test_scheduler_completion_reads_job_draft_and_step_ids(tmp_path: Path) -> None:
    profiles, profile, store, context, _, _ = _store(tmp_path, job_id="active-job")
    command = default_registry().validate("eodhd", "reindex", [], context).spec
    store.put_draft(
        JobDraft(
            draft_id="draft-job",
            profile_id=profile.profile_id,
            job_id="draft-job",
            display_name="Draft",
            trigger=TriggerSpec.manual(),
            steps=(command, command),
        )
    )
    common = {
        "repo_root": REPO,
        "config_path": Path(profile.config_path),
        "state_root": profiles.state_root,
    }
    assert "active-job" in schedule_completion_candidates(["run", ""], **common)
    assert "active-job" in schedule_completion_candidates(
        [
            "--repo-root",
            str(REPO),
            "--config",
            str(profile.config_path),
            "run",
            "",
        ],
        repo_root=tmp_path / "wrong-repo",
        config_path=tmp_path / "wrong.toml",
        state_root=profiles.state_root,
    )
    assert "draft-job" in schedule_completion_candidates(["enable", ""], **common)
    assert profile.profile_id in schedule_completion_candidates(
        ["--profile-id", ""], state_root=profiles.state_root
    )
    assert schedule_completion_candidates(
        ["step", "remove", "draft-job", ""], **common
    ) == ("1", "2")
