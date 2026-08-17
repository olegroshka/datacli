"""Windows Task Scheduler 2.0 XML adapter using argument-array schtasks calls."""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
import uuid
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from typing import Callable, Sequence

from ..model import BackendTaskState, DispatchReceipt, JobSpec, utc_now
from ..store import _secure
from .base import CancellationReceipt, RunnerAction

TASK_NS = "http://schemas.microsoft.com/windows/2004/02/mit/task"
ET.register_namespace("", TASK_NS)
META_RE = re.compile(
    r"^datacli profile=(?P<profile>[0-9a-f-]+) job=(?P<job>[a-z0-9-]+) "
    r"generation=(?P<generation>\d+) digest=(?P<digest>[0-9a-f]{64}) "
    r"timezone=(?P<timezone>.+)$"
)
OBSERVATION_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$name = $env:DATACLI_TASK_NAME
$matches = @(Get-ScheduledTask -ErrorAction Stop | Where-Object {
  $_.TaskPath -eq '\' -and $_.TaskName -ceq $name
})
if ($matches.Count -eq 0) {
  [pscustomobject]@{ Exists = $false } | ConvertTo-Json -Compress
  exit 0
}
if ($matches.Count -ne 1) { throw 'ambiguous root task identity' }
$task = $matches[0]
$info = Get-ScheduledTaskInfo -InputObject $task -ErrorAction Stop
$log = Get-WinEvent -ListLog 'Microsoft-Windows-TaskScheduler/Operational' -ErrorAction SilentlyContinue
$result = [int64]$info.LastTaskResult
[pscustomobject]@{
  Exists = $true
  State = $task.State.ToString()
  Enabled = [bool]$task.Settings.Enabled
  NextRunTime = if ($info.NextRunTime) { $info.NextRunTime.ToString('o') } else { $null }
  LastRunTime = if ($result -ne 267011 -and $info.LastRunTime -and $info.LastRunTime.Year -gt 1900) { $info.LastRunTime.ToString('o') } else { $null }
  LastTaskResult = $result
  NumberOfMissedRuns = [int]$info.NumberOfMissedRuns
  HistoryAvailable = if ($null -eq $log) { $null } else { [bool]$log.IsEnabled }
} | ConvertTo-Json -Compress
""".strip()

RESULT_CODES = {
    0: "datacli workflow succeeded or was a typed no-op",
    1: "datacli workflow step failed",
    2: "datacli definition/config/preflight was invalid",
    3: "datacli run was cancelled or timed out",
    69: "datacli runtime environment was unavailable",
    75: "datacli runner skipped an overlap",
    76: "datacli runner rejected a stale dispatch",
    78: "datacli runner could not establish its journal",
    267011: "Task Scheduler reports that the task has not yet run",
}


def _q(name: str) -> str:
    return f"{{{TASK_NS}}}{name}"


def task_name(profile_id: str, job_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f-]{36}", profile_id):
        raise ValueError("invalid profile id for task name")
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", job_id):
        raise ValueError("invalid job id for task name")
    value = f"Datacli-{profile_id[:12]}-{job_id}"
    if len(value) > 238:
        raise ValueError("derived task name is too long")
    return value


def _duration(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"PT{hours}H{minutes}M{secs}S"


def _failure_detail(result: subprocess.CompletedProcess[str]) -> str:
    detail = " ".join((result.stderr or result.stdout or "").split())
    return f": {detail[:500]}" if detail else ""


def _text(parent: ET.Element, name: str, value: object) -> ET.Element:
    child = ET.SubElement(parent, _q(name))
    child.text = str(value)
    return child


def build_task_xml(spec: JobSpec, action: RunnerAction, *, user_id: str) -> bytes:
    root = ET.Element(_q("Task"), {"version": "1.4"})
    registration = ET.SubElement(root, _q("RegistrationInfo"))
    timezone_name = spec.trigger.timezone_at_validation or "system-local"
    _text(
        registration,
        "Description",
        f"datacli profile={spec.profile_id} job={spec.job_id} "
        f"generation={spec.generation} digest={spec.digest} timezone={timezone_name}",
    )
    _text(registration, "URI", f"\\{task_name(spec.profile_id, spec.job_id)}")

    triggers = ET.SubElement(root, _q("Triggers"))
    if spec.trigger.kind in {"daily", "weekly"}:
        calendar = ET.SubElement(triggers, _q("CalendarTrigger"))
        _text(
            calendar,
            "StartBoundary",
            f"{date.today().isoformat()}T{spec.trigger.local_time}:00",
        )
        _text(calendar, "Enabled", "true")
        if spec.trigger.kind == "daily":
            schedule = ET.SubElement(calendar, _q("ScheduleByDay"))
            _text(schedule, "DaysInterval", 1)
        else:
            schedule = ET.SubElement(calendar, _q("ScheduleByWeek"))
            days = ET.SubElement(schedule, _q("DaysOfWeek"))
            for day in spec.trigger.days_of_week:
                ET.SubElement(days, _q(day.title()))
            _text(schedule, "WeeksInterval", 1)

    principals = ET.SubElement(root, _q("Principals"))
    principal = ET.SubElement(principals, _q("Principal"), {"id": "Author"})
    _text(principal, "UserId", user_id)
    _text(principal, "LogonType", "InteractiveToken")
    _text(principal, "RunLevel", "LeastPrivilege")

    settings = ET.SubElement(root, _q("Settings"))
    _text(settings, "MultipleInstancesPolicy", "Parallel")
    _text(settings, "DisallowStartIfOnBatteries", str(spec.trigger.ac_only).lower())
    _text(settings, "StopIfGoingOnBatteries", "false")
    _text(settings, "AllowHardTerminate", "true")
    _text(
        settings, "StartWhenAvailable", str(spec.trigger.start_when_available).lower()
    )
    _text(settings, "RunOnlyIfNetworkAvailable", "false")
    idle = ET.SubElement(settings, _q("IdleSettings"))
    _text(idle, "StopOnIdleEnd", "false")
    _text(idle, "RestartOnIdle", "false")
    _text(settings, "AllowStartOnDemand", "true")
    _text(settings, "Enabled", str(spec.enabled).lower())
    _text(settings, "Hidden", "false")
    _text(settings, "RunOnlyIfIdle", "false")
    _text(settings, "WakeToRun", str(spec.trigger.wake_to_run).lower())
    _text(
        settings,
        "ExecutionTimeLimit",
        _duration(spec.policy.execution_timeout_seconds + 3600),
    )
    _text(settings, "Priority", 7)

    actions = ET.SubElement(root, _q("Actions"), {"Context": "Author"})
    execute = ET.SubElement(actions, _q("Exec"))
    _text(execute, "Command", str(action.command))
    _text(execute, "Arguments", subprocess.list2cmdline(list(action.arguments)))
    _text(execute, "WorkingDirectory", str(action.working_directory))
    return ET.tostring(root, encoding="utf-16", xml_declaration=True)


class WindowsTaskSchedulerBackend:
    def __init__(
        self,
        state_root: Path,
        *,
        invoker: (
            Callable[[Sequence[str]], subprocess.CompletedProcess[str]] | None
        ) = None,
        observer: Callable[[str], subprocess.CompletedProcess[str]] | None = None,
        user_id: str | None = None,
    ) -> None:
        self.state_root = state_root.resolve()
        self.invoker = invoker or self._invoke
        self.observer = observer or self._observe_powershell
        self.user_id = user_id or self._current_user()

    @staticmethod
    def _current_user() -> str:
        # USERDOMAIN/USERNAME and getpass.getuser() can describe the interactive
        # desktop owner rather than the effective process token (for example in
        # a sandbox or run-as session). Task Scheduler needs the latter.
        if os.name == "nt":
            try:
                result = subprocess.run(
                    ["whoami.exe"],
                    shell=False,
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
                identity = result.stdout.strip()
                if result.returncode == 0 and identity and "\n" not in identity:
                    return identity
            except OSError:
                pass
        domain = os.environ.get("USERDOMAIN")
        user = os.environ.get("USERNAME")
        if not user:
            raise RuntimeError("could not resolve the effective Windows user")
        return f"{domain}\\{user}" if domain else user

    @staticmethod
    def _invoke(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        if os.name != "nt":
            raise RuntimeError(
                "Windows Task Scheduler backend is available only on Windows"
            )
        return subprocess.run(
            list(args),
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    @staticmethod
    def _observe_powershell(name: str) -> subprocess.CompletedProcess[str]:
        if os.name != "nt":
            raise RuntimeError(
                "Windows Task Scheduler backend is available only on Windows"
            )
        environment = dict(os.environ)
        environment["DATACLI_TASK_NAME"] = name
        return subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                OBSERVATION_SCRIPT,
            ],
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
        )

    def install(self, spec: JobSpec, action: RunnerAction) -> BackendTaskState:
        payload = build_task_xml(spec, action, user_id=self.user_id)
        temp_root = self.state_root / "backend-temp"
        temp_root.mkdir(parents=True, exist_ok=True)
        _secure(temp_root, directory=True)
        path = temp_root / f"task-{uuid.uuid4().hex}.xml"
        try:
            with path.open("xb") as stream:
                _secure(path)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            result = self.invoker(
                [
                    "schtasks.exe",
                    "/Create",
                    "/TN",
                    task_name(spec.profile_id, spec.job_id),
                    "/XML",
                    str(path),
                    "/F",
                ]
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"Task Scheduler registration failed (exit {result.returncode})"
                    f"{_failure_detail(result)}"
                )
        finally:
            with contextlib.suppress(FileNotFoundError):
                path.unlink()
        return self.query(spec.profile_id, spec.job_id)

    def _change(self, profile_id: str, job_id: str, switch: str) -> BackendTaskState:
        result = self.invoker(
            ["schtasks.exe", "/Change", "/TN", task_name(profile_id, job_id), switch]
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Task Scheduler change failed (exit {result.returncode})"
                f"{_failure_detail(result)}"
            )
        return self.query(profile_id, job_id)

    def enable(self, profile_id: str, job_id: str) -> BackendTaskState:
        return self._change(profile_id, job_id, "/ENABLE")

    def disable(self, profile_id: str, job_id: str) -> BackendTaskState:
        return self._change(profile_id, job_id, "/DISABLE")

    def remove(self, profile_id: str, job_id: str) -> BackendTaskState:
        result = self.invoker(
            ["schtasks.exe", "/Delete", "/TN", task_name(profile_id, job_id), "/F"]
        )
        if result.returncode != 0:
            state = self.query(profile_id, job_id)
            if state.exists or state.drift:
                raise RuntimeError(
                    f"Task Scheduler delete failed (exit {result.returncode})"
                    f"{_failure_detail(result)}"
                )
        return self.query(profile_id, job_id)

    def run_now(self, profile_id: str, job_id: str) -> DispatchReceipt:
        result = self.invoker(
            ["schtasks.exe", "/Run", "/TN", task_name(profile_id, job_id)]
        )
        accepted = result.returncode == 0
        return DispatchReceipt(
            requested_at=utc_now(),
            accepted=accepted,
            backend_token=None,
            raw_result=result.returncode,
            message=(
                "Task Scheduler accepted the dispatch request; execution is not yet proven"
                if accepted
                else f"Task Scheduler rejected the dispatch request (exit {result.returncode})"
            ),
        )

    def stop(self, profile_id: str, job_id: str) -> CancellationReceipt:
        result = self.invoker(
            ["schtasks.exe", "/End", "/TN", task_name(profile_id, job_id)]
        )
        accepted = result.returncode == 0
        return CancellationReceipt(
            requested_at=utc_now(),
            accepted=accepted,
            confirmed_terminal=None,
            raw_result=result.returncode,
            message=(
                "Task Scheduler accepted cancellation; process-tree termination remains unverified"
                if accepted
                else f"Task Scheduler rejected cancellation (exit {result.returncode})"
            ),
        )

    def query(self, profile_id: str, job_id: str) -> BackendTaskState:
        name = task_name(profile_id, job_id)
        result = self.invoker(["schtasks.exe", "/Query", "/TN", name, "/XML"])
        if result.returncode != 0:
            try:
                observed = self.observer(name)
                if observed.returncode == 0:
                    runtime = json.loads(observed.stdout)
                    if runtime.get("Exists") is False:
                        return BackendTaskState(
                            exists=False,
                            state="missing",
                            history_available=None,
                        )
                    if runtime.get("Exists") is True:
                        return BackendTaskState(
                            exists=True,
                            state="unknown",
                            history_available=None,
                            drift=(f"query_failed_exit_{result.returncode}",),
                        )
                observation_drift = (
                    "runtime_observation_invalid_payload"
                    if observed.returncode == 0
                    else f"runtime_observation_failed_exit_{observed.returncode}"
                )
            except (
                OSError,
                RuntimeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                observation_drift = (
                    f"runtime_observation_unavailable:{type(exc).__name__}"
                )
            return BackendTaskState(
                exists=False,
                state="unknown",
                history_available=None,
                drift=(
                    f"query_failed_exit_{result.returncode}",
                    observation_drift,
                ),
            )
        try:
            root = ET.fromstring(result.stdout)
            description = (
                root.findtext(f"./{_q('RegistrationInfo')}/{_q('Description')}") or ""
            )
            match = META_RE.fullmatch(description)
            enabled_text = root.findtext(f"./{_q('Settings')}/{_q('Enabled')}")
            command = root.findtext(f"./{_q('Actions')}/{_q('Exec')}/{_q('Command')}")
            arguments = root.findtext(
                f"./{_q('Actions')}/{_q('Exec')}/{_q('Arguments')}"
            )
            drift: list[str] = []
            generation = None
            digest = None
            if match:
                if match.group("profile") != profile_id or match.group("job") != job_id:
                    drift.append("ownership_metadata_mismatch")
                generation = int(match.group("generation"))
                digest = match.group("digest")
            else:
                drift.append("missing_or_invalid_datacli_metadata")
            if not command or not Path(command).is_absolute():
                drift.append("missing_or_nonabsolute_interpreter")
            if not arguments or "scheduler.runner" not in arguments:
                drift.append("runner_action_mismatch")
            enabled = enabled_text.casefold() == "true" if enabled_text else None
            runtime_state = "unknown"
            raw_state = None
            next_run = None
            last_run = None
            last_result = None
            missed = None
            history = None
            try:
                observed = self.observer(name)
                if observed.returncode == 0:
                    runtime = json.loads(observed.stdout)
                    raw_state = str(runtime.get("State") or "Unknown")
                    runtime_state = {
                        "ready": "ready",
                        "running": "running",
                        "disabled": "disabled",
                        "queued": "queued",
                    }.get(raw_state.casefold(), "unknown")
                    enabled = (
                        runtime["Enabled"]
                        if isinstance(runtime.get("Enabled"), bool)
                        else enabled
                    )
                    next_run = runtime.get("NextRunTime")
                    last_run = runtime.get("LastRunTime")
                    last_result = runtime.get("LastTaskResult")
                    missed = runtime.get("NumberOfMissedRuns")
                    history = runtime.get("HistoryAvailable")
                else:
                    drift.append(
                        f"runtime_observation_failed_exit_{observed.returncode}"
                    )
            except (
                OSError,
                RuntimeError,
                json.JSONDecodeError,
                TypeError,
                ValueError,
            ) as exc:
                drift.append(f"runtime_observation_unavailable:{type(exc).__name__}")
            return BackendTaskState(
                exists=True,
                enabled=enabled,
                state=runtime_state,
                raw_state=raw_state,
                next_run_at=next_run,
                last_run_at=last_run,
                last_result_raw=last_result,
                last_result_decoded=(
                    RESULT_CODES.get(int(last_result))
                    if isinstance(last_result, (int, float))
                    else None
                ),
                installed_digest=digest,
                installed_generation=generation,
                missed_run_count=(
                    int(missed) if isinstance(missed, (int, float)) else None
                ),
                history_available=history if isinstance(history, bool) else None,
                drift=tuple(drift),
            )
        except (ET.ParseError, ValueError) as exc:
            return BackendTaskState(
                exists=True,
                state="unknown",
                history_available=None,
                drift=(f"invalid_task_xml:{type(exc).__name__}",),
            )
