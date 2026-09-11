"""Runner runtime behaviour added after the 2026-09-09/10/11 incidents:
awake-clock timeouts, the system-required power request, status files and the
optional notify command. Reuses the fixtures from test_scheduler."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from test_scheduler import _ResultRegistry, _store  # noqa: E402

from scheduler import notify, power  # noqa: E402
from scheduler import runner as runner_module  # noqa: E402
from scheduler.commands import CommandResult  # noqa: E402
from scheduler.journal import RunJournal  # noqa: E402
from scheduler.runner import JobRunner  # noqa: E402

HOUR = 3600.0


def _fake_clock(monkeypatch: pytest.MonkeyPatch, readings: list[float]) -> None:
    """awake_clock() returns the scripted readings, then keeps the last one."""
    values = list(readings)

    def clock() -> float:
        if len(values) > 1:
            return values.pop(0)
        return values[0]

    monkeypatch.setattr(runner_module, "awake_clock", clock)


def _fake_power(monkeypatch: pytest.MonkeyPatch, *, held: bool) -> list[str]:
    events: list[str] = []

    class _Request:
        def __enter__(self) -> bool:
            events.append("enter")
            return held

        def __exit__(self, *exc) -> None:
            events.append("exit")

    monkeypatch.setattr(runner_module, "keep_system_awake", lambda: _Request())
    return events


def _two_ok() -> _ResultRegistry:
    return _ResultRegistry(
        [
            CommandResult("succeeded", 0, effect="complete"),
            CommandResult("succeeded", 0, effect="complete"),
        ]
    )


def test_timeout_is_measured_on_awake_clock_not_calendar_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 16 h calendar run with 10 h of sleep is a 6 h run: both steps execute."""
    _, profile, store, _, pointer, _ = _store(tmp_path, steps=2)
    # readings: run start, before step 1, before step 2 (6 h awake by then)
    _fake_clock(monkeypatch, [0.0, 0.0, 6 * HOUR])
    events = _fake_power(monkeypatch, held=True)
    registry = _two_ok()

    record = JobRunner(store, registry=registry).execute(
        "morning", 1, pointer.digest, dispatch_kind="foreground_test"
    )

    assert record.outcome == "succeeded"
    assert [step.outcome for step in record.steps] == ["succeeded", "succeeded"]
    assert registry.calls == 2
    assert events == ["enter", "exit"]
    journal = RunJournal(profile, store.state_root).events("morning", record.run_id)
    power_events = [e for e in journal if e["event"] == "power_request"]
    assert power_events and power_events[0]["payload"] == {"system_required": True}


def test_awake_budget_exhausted_still_times_out_at_the_step_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, store, _, pointer, _ = _store(tmp_path, steps=2)
    _fake_clock(monkeypatch, [0.0, 0.0, 13 * HOUR])  # over the 12 h default
    _fake_power(monkeypatch, held=False)
    registry = _two_ok()

    record = JobRunner(store, registry=registry).execute(
        "morning", 1, pointer.digest, dispatch_kind="foreground_test"
    )

    assert record.outcome == "timed_out"
    assert [step.outcome for step in record.steps] == ["succeeded", "not_run"]
    assert registry.calls == 1


def test_power_request_is_released_even_when_a_step_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, store, _, pointer, _ = _store(tmp_path)
    _fake_clock(monkeypatch, [0.0])
    events = _fake_power(monkeypatch, held=True)

    class _Boom(_ResultRegistry):
        def execute(self, *args, **kwargs):
            raise RuntimeError("adapter exploded")

    with pytest.raises(RuntimeError):
        JobRunner(store, registry=_Boom([])).execute(
            "morning", 1, pointer.digest, dispatch_kind="foreground_test"
        )
    assert events == ["enter", "exit"]


def test_keep_system_awake_sets_and_clears_execution_state() -> None:
    calls: list[int] = []

    def setter(flags: int) -> int | None:
        calls.append(flags)
        return 0x80000000  # previous state, as Windows returns it

    with power.keep_system_awake(setter=setter) as held:
        assert held is True
    assert calls == [
        power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED,
        power.ES_CONTINUOUS,
    ]

    failing: list[int] = []

    def refuse(flags: int) -> int | None:
        failing.append(flags)
        return None

    with power.keep_system_awake(setter=refuse) as held:
        assert held is False
    assert failing == [power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED]  # no clear


def test_awake_clock_never_exceeds_monotonic() -> None:
    import time

    assert power.awake_clock() <= time.monotonic() + 1.0


# --------------------------------------------------------------------------- #
# notifications
# --------------------------------------------------------------------------- #
def test_failed_run_writes_status_files_and_journals_the_notification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path, steps=2)
    _fake_clock(monkeypatch, [0.0])
    _fake_power(monkeypatch, held=False)
    registry = _ResultRegistry(
        [
            CommandResult("failed", 1, failure_class="command_exit", effect="partial"),
            CommandResult("succeeded", 0, effect="complete"),
        ]
    )

    record = JobRunner(store, registry=registry).execute(
        "morning", 1, pointer.digest, dispatch_kind="foreground_test"
    )

    assert record.outcome == "failed"
    job_dir = Path(record.journal_path).parent.parent
    last_run = (job_dir / notify.STATUS_FILE).read_text(encoding="utf-8")
    last_failure = (job_dir / notify.FAILURE_FILE).read_text(encoding="utf-8")
    assert last_run == last_failure
    assert f" morning {record.run_id} failed steps=1:failed,2:not_run log=" in last_run
    journal = RunJournal(profile, store.state_root).events("morning", record.run_id)
    assert journal[-1]["event"] == "run_terminal"
    assert journal[-1]["payload"]["notified"]["command"] is None
    assert len(journal[-1]["payload"]["notified"]["files"]) == 2


def test_successful_run_updates_last_run_but_not_last_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, store, _, pointer, _ = _store(tmp_path)
    _fake_clock(monkeypatch, [0.0])
    _fake_power(monkeypatch, held=False)
    record = JobRunner(
        store,
        registry=_ResultRegistry([CommandResult("succeeded", 0, effect="complete")]),
    ).execute("morning", 1, pointer.digest, dispatch_kind="foreground_test")

    job_dir = Path(record.journal_path).parent.parent
    assert (job_dir / notify.STATUS_FILE).exists()
    assert not (job_dir / notify.FAILURE_FILE).exists()


def test_notify_command_runs_for_failures_only_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, profile, store, _, pointer, _ = _store(tmp_path)
    marker = tmp_path / "notified.json"
    script = tmp_path / "notify.py"
    script.write_text(
        "import json, sys\n"
        f"open({str(marker)!r}, 'w').write(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    # Editing datacli.toml after install would be a binding drift (the run
    # becomes `invalid`), so the settings are injected instead; TOML parsing
    # is covered separately below.
    monkeypatch.setattr(
        notify,
        "load_notify_settings",
        lambda _path: {
            "notify_command": [
                sys.executable,
                str(script),
                "{job_id}",
                "{outcome}",
                "{summary}",
            ]
        },
    )
    _fake_clock(monkeypatch, [0.0])
    _fake_power(monkeypatch, held=False)

    ok = JobRunner(
        store,
        registry=_ResultRegistry([CommandResult("succeeded", 0, effect="complete")]),
    ).execute("morning", 1, pointer.digest, dispatch_kind="foreground_test")
    assert ok.outcome == "succeeded"
    assert not marker.exists()  # notify_on defaults to failure
    events = RunJournal(profile, store.state_root).events("morning", ok.run_id)
    assert events[-1]["payload"]["notified"]["command"] == {
        "status": "skipped",
        "reason": "notify_on=failure",
    }

    failed = JobRunner(
        store,
        registry=_ResultRegistry(
            [CommandResult("failed", 2, failure_class="command_exit", effect="partial")]
        ),
    ).execute("morning", 1, pointer.digest, dispatch_kind="foreground_test")
    argv = json.loads(marker.read_text(encoding="utf-8"))
    assert argv[:2] == ["morning", "failed"]
    assert argv[2] == notify.status_line(failed)
    events = RunJournal(profile, store.state_root).events("morning", failed.run_id)
    command = events[-1]["payload"]["notified"]["command"]
    assert command["status"] == "ran" and command["returncode"] == 0


def test_render_command_quotes_string_templates_and_formats_lists() -> None:
    from scheduler.model import RunRecord

    record = RunRecord(
        run_id="r1",
        profile_id="p",
        job_id="job",
        definition_digest="d",
        definition_generation=1,
        definition_snapshot_ref=None,
        snapshot_status="loaded",
        dispatch_kind="backend",
        backend_cause_hint=None,
        scheduled_for_hint=None,
        observed_started_at="t0",
        finished_at="t1",
        outcome="failed",
        steps=(),
        log_path="C:/logs/run.log",
        journal_path="C:/state/runs/job/r1/events.jsonl",
        runtime_binding_fingerprint="",
        runner_version="v",
        command_contract_version="c",
    )
    rendered = notify.render_command('notify {job_id} {outcome} "{log_path}"', record)
    assert rendered == 'notify "job" "failed" ""C:/logs/run.log""'
    rendered_list = notify.render_command(["x", "{job_id}:{outcome}"], record)
    assert rendered_list == ["x", "job:failed"]
    assert notify.status_line(record) == "t1 job r1 failed steps=- log=C:/logs/run.log"


def test_load_notify_settings_reads_scheduler_section(tmp_path: Path) -> None:
    config = tmp_path / "datacli.toml"
    config.write_text(
        "\n".join(
            [
                "[eodhd]",
                'data_root = "x"',
                "",
                "[scheduler]",
                'notify_on = "always"',
                'notify_command = ["a", "{outcome}"]',
                "",
            ]
        ),
        encoding="utf-8",
    )
    assert notify.load_notify_settings(config) == {
        "notify_on": "always",
        "notify_command": ["a", "{outcome}"],
    }
    assert notify.load_notify_settings(tmp_path / "missing.toml") == {}
    assert notify.load_notify_settings(None) == {}
    config.write_text("not = [toml", encoding="utf-8")
    assert notify.load_notify_settings(config) == {}
