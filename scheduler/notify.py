"""Tell someone when a run ends: status files, plus an optional command.

Nobody should learn about a failed push by looking at Drive. After every
terminal outcome the runner:

1. writes ``LAST_RUN.txt`` next to the job's run directories (one line:
   finished-at, job, run id, outcome, per-step outcomes, log path), and
   ``LAST_FAILURE.txt`` with the same line whenever the outcome is not
   ``succeeded``/``no_op``;
2. runs ``[scheduler] notify_command`` from ``datacli.toml`` when configured.

``notify_command`` is either a string (run through the shell) or an array of
arguments (run directly, safest on Windows). Placeholders ``{job_id}``,
``{run_id}``, ``{outcome}``, ``{log_path}`` and ``{summary}`` are substituted
in every element; string commands get each value double-quoted. It runs for
failures only unless ``[scheduler] notify_on = "always"``.

    [scheduler]
    notify_command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-File", "C:/path/to/datacli/scripts/notify_toast.ps1",
                      "-Title", "datacli {job_id}: {outcome}",
                      "-Message", "{summary}"]

Everything here is best effort and never raises into the runner; the result
is journaled so a broken notifier is itself visible.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path
from typing import Any, Mapping

from .model import RunRecord

STATUS_FILE = "LAST_RUN.txt"
FAILURE_FILE = "LAST_FAILURE.txt"
OK_OUTCOMES = frozenset({"succeeded", "no_op"})
COMMAND_TIMEOUT_SECONDS = 60


def status_line(record: RunRecord) -> str:
    steps = ",".join(f"{step.index}:{step.outcome}" for step in record.steps) or "-"
    return (
        f"{record.finished_at} {record.job_id} {record.run_id} {record.outcome} "
        f"steps={steps} log={record.log_path}"
    )


def job_dir(record: RunRecord) -> Path:
    """``.../runs/<job_id>`` derived from the record's own journal path."""
    return Path(record.journal_path).resolve().parent.parent


def write_status_files(record: RunRecord) -> list[str]:
    line = status_line(record) + "\n"
    target = job_dir(record)
    target.mkdir(parents=True, exist_ok=True)
    written = [target / STATUS_FILE]
    if record.outcome not in OK_OUTCOMES:
        written.append(target / FAILURE_FILE)
    for path in written:
        path.write_text(line, encoding="utf-8")
    return [str(path) for path in written]


def load_notify_settings(config_path: Path | str | None) -> dict[str, Any]:
    if config_path is None:
        return {}
    path = Path(config_path)
    if not path.exists():
        return {}
    try:
        value = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = value.get("scheduler")
    return dict(section) if isinstance(section, dict) else {}


def _placeholders(record: RunRecord) -> dict[str, str]:
    return {
        "job_id": record.job_id,
        "run_id": record.run_id,
        "outcome": record.outcome,
        "log_path": record.log_path,
        "summary": status_line(record),
    }


def render_command(template: str | list[str], record: RunRecord) -> str | list[str]:
    values = _placeholders(record)
    if isinstance(template, str):
        quoted = {k: '"' + v.replace('"', "'") + '"' for k, v in values.items()}
        return template.format_map(quoted)
    return [str(part).format_map(values) for part in template]


def run_notify_command(
    template: str | list[str],
    record: RunRecord,
    *,
    environment: Mapping[str, str],
    timeout: float = COMMAND_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    command = render_command(template, record)
    try:
        completed = subprocess.run(
            command,
            shell=isinstance(command, str),
            env=dict(environment),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return {
        "status": "ran" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "stderr": (completed.stderr or "")[-500:],
    }


def notify_terminal(
    record: RunRecord,
    *,
    config_path: Path | str | None,
    environment: Mapping[str, str],
) -> dict[str, Any]:
    """Write the status files and run the configured command. Never raises."""
    result: dict[str, Any] = {"files": [], "command": None}
    try:
        result["files"] = write_status_files(record)
    except OSError as exc:
        result["files_error"] = f"{type(exc).__name__}: {exc}"
    settings = load_notify_settings(config_path)
    template = settings.get("notify_command")
    if not template:
        return result
    if isinstance(template, list) and not all(isinstance(p, str) for p in template):
        result["command"] = {
            "status": "error",
            "error": "notify_command must be str or list[str]",
        }
        return result
    when = str(settings.get("notify_on") or "failure")
    if when != "always" and record.outcome in OK_OUTCOMES:
        result["command"] = {"status": "skipped", "reason": f"notify_on={when}"}
        return result
    try:
        result["command"] = run_notify_command(
            template, record, environment=environment
        )
    except Exception as exc:  # noqa: BLE001 - notifier must never break the run
        result["command"] = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
    return result
