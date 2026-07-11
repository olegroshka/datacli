"""Restricted local Python executor for the lab (opt-in, off by default).

Runs LLM-written analysis code in a child process with a hard wall-clock timeout,
a restricted builtin/import environment and a network block (see
``_pyexec_runner.py``). The input is a DataFrame (a grounded query result); the
outputs are captured stdout and, optionally, a saved matplotlib figure.

This is a *trusted-local* convenience -- honestly NOT a security sandbox. It is
enabled only when ``[lab].allow_python`` is true and the persona has the
``run_python`` tool.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]


@dataclass
class PyExecResult:
    ok: bool
    stdout: str = ""
    error: str = ""
    figure_path: str | None = None


def run_code(
    code: str,
    df: Any = None,
    *,
    timeout_s: int = 20,
    figure_dir: Path | None = None,
    figure_name: str | None = None,
) -> PyExecResult:
    """Execute ``code`` with ``df`` available, in a timed, restricted subprocess.

    If the code draws a matplotlib figure and ``figure_dir`` is given, the PNG is
    copied there (as ``figure_name`` or ``figure.png``) and its path returned.
    """
    workdir = Path(tempfile.mkdtemp(prefix="lab_pyexec_"))
    try:
        (workdir / "code.py").write_text(code, encoding="utf-8")
        if df is not None:
            try:
                df.to_parquet(workdir / "input.parquet", index=False)
            except Exception as exc:
                return PyExecResult(ok=False, error=f"could not pass dataframe: {exc}")

        try:
            # run as a module from the repo root so `lab/` is NOT on sys.path[0]
            # (otherwise lab/types.py shadows the stdlib `types` module).
            proc = subprocess.run(
                [sys.executable, "-m", "lab._pyexec_runner", str(workdir)],
                cwd=str(_REPO),
                timeout=timeout_s,
                capture_output=True,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return PyExecResult(ok=False, error=f"timed out after {timeout_s}s")

        result_path = workdir / "result.json"
        if not result_path.exists():
            err = (proc.stderr or "the executor produced no result").strip()
            return PyExecResult(ok=False, error=err[:2000])

        data = json.loads(result_path.read_text(encoding="utf-8"))
        figure_path: str | None = None
        if data.get("figure"):
            src = workdir / "figure.png"
            if figure_dir is not None:
                figure_dir = Path(figure_dir)
                figure_dir.mkdir(parents=True, exist_ok=True)
                dest = figure_dir / (figure_name or "figure.png")
                shutil.copyfile(src, dest)
                figure_path = str(dest)
            else:
                figure_path = str(src)  # left in the temp dir (not cleaned below)

        return PyExecResult(
            ok=bool(data.get("ok")),
            stdout=str(data.get("stdout", "")),
            error=str(data.get("error", "")),
            figure_path=figure_path,
        )
    finally:
        # keep the temp dir only when a figure was left inside it
        keep = (workdir / "figure.png").exists() and figure_dir is None
        if not keep:
            shutil.rmtree(workdir, ignore_errors=True)
