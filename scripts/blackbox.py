#!/usr/bin/env python
"""Black-box scenario harness for datacli — a repeatable test *and* a demo.

Drives the REAL command entry points as subprocesses (a true black box: real
process, real output, real data), captures each command's output, checks
expectations, and logs a JSONL + a Markdown transcript under ``.datacli_logs/``.

    uv run python scripts/blackbox.py --check          # assert + exit code (CI)
    uv run python scripts/blackbox.py --demo           # slow-motion, shell-styled
    uv run python scripts/blackbox.py --check --live   # include the LLM scenarios
    uv run python scripts/blackbox.py --only S1,S2     # subset

Scenarios mirror SCENARIOS.md. LLM/paid steps are gated behind ``--live``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
LOG_DIR = REPO / ".datacli_logs"

# command head -> which entry point runs it
_EODHD = {
    "status",
    "qc",
    "describe",
    "find",
    "rows",
    "coverage",
    "sql",
    "schema",
    "reindex",
    "lanes",
    "config",
    "probe",
    "refresh",
}
_LAB_TOP = {"ask", "agent", "investigate"}


# --------------------------------------------------------------------------- #
# scenario model
# --------------------------------------------------------------------------- #
@dataclass
class Step:
    cmd: str  # shell-style command, e.g. "status us_common"
    expect: tuple[str, ...] = ()  # case-insensitive substrings that must appear
    note: str = ""  # demo narration
    live: bool = False  # needs a model / paid API -> only with --live


@dataclass
class Scenario:
    key: str
    title: str
    intent: str
    steps: list[Step] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        "S1",
        "Monday-morning triage",
        "Am I safe to build on this data?",
        [
            Step(
                "status",
                ("fresh", "stale", "rows"),
                "One screen: freshness across every lane.",
            ),
            Step(
                "qc us_common",
                ("errors", "warnings", "action"),
                "Ranked issues with the recommended fix.",
            ),
            Step(
                "qc us_common splits",
                ("splits", "empty_with_rows"),
                "Drill into one dataset, uncapped.",
            ),
        ],
    ),
    Scenario(
        "S2",
        "Entity lookup",
        "Do we have VAR.OL, and how well?",
        [
            Step(
                "describe VAR.OL",
                ("fundamentals", "VAR.OL"),
                "The whole cross-dataset question in one command.",
            ),
            Step(
                "find VAR",
                ("matches for 'VAR'",),
                "Fuzzy match when you don't recall the exact symbol.",
            ),
            Step(
                "coverage VAR.OL",
                ("coverage", "fundamentals"),
                "Per-dataset coverage windows.",
            ),
        ],
    ),
    Scenario(
        "S3",
        "Onboarding & forgiveness",
        "Point at my data; be forgiving.",
        [
            Step(
                "config",
                ("data-root", "lanes"),
                "Where's my data, resolved with its source.",
            ),
            Step(
                "schema",
                ("SCHEMA_VERSION", "canonical"),
                "Drift-safe: extra columns are fine.",
            ),
            Step(
                'sql "SELECT lane, count(*) AS n FROM dividends GROUP BY lane ORDER BY n DESC"',
                ("us_common", "n"),
                "Ad-hoc query, no boilerplate.",
            ),
            Step(
                "qc us_comon",
                ("did you mean", "us_common"),
                "A typo is met with a suggestion, not a stack trace.",
            ),
        ],
    ),
    Scenario(
        "S4",
        "Natural-language exploration",
        "I think in questions, not SQL.",
        [
            Step(
                'ask "how many dividend rows are there per lane?"',
                ("answer",),
                "A simple grounded question -- shows the SQL it wrote.",
                live=True,
            ),
            Step(
                'ask "which lanes have the worst dividend coverage, and why?"',
                ("answer",),
                "A harder one -- grounded, no fabricated numbers.",
                live=True,
            ),
        ],
    ),
    Scenario(
        "S5",
        "Synthesis & artifact",
        "Macro context + a shareable report.",
        [
            Step(
                "macro list",
                ("FRED series", "EODHD indicators"),
                "Two macro providers, one registry.",
            ),
            Step("macro status", ("macro",), "What macro data is on disk."),
            Step(
                'investigate "relate dividend coverage across lanes to fetch freshness"',
                ("synthesis",),
                "generator -> skeptic -> reporter -> report.",
                live=True,
            ),
        ],
    ),
]


# --------------------------------------------------------------------------- #
# running
# --------------------------------------------------------------------------- #
def _full_argv(cmd: str) -> list[str]:
    """Map a shell-style command to a real subprocess argv."""
    toks = shlex.split(cmd)
    head = toks[0]
    if head == "macro":
        return [sys.executable, "-m", "macro.cli", *toks[1:]]
    if head == "lab":
        return [sys.executable, "-m", "lab.cli", *toks[1:]]
    if head in _LAB_TOP:
        return [sys.executable, "-m", "lab.cli", *toks]
    if head in _EODHD:
        return [sys.executable, "eodhd/cli.py", *toks]
    raise ValueError(f"unknown command head: {head!r}")


@dataclass
class Result:
    step: Step
    argv: list[str]
    output: str
    duration_s: float
    returncode: int
    status: str  # PASS | FAIL | BOUNDARY
    checks: list[tuple[str, bool]] = field(default_factory=list)


def run_step(step: Step, *, timeout: int = 120) -> Result:
    argv = _full_argv(step.cmd)
    # The commands emit UTF-8 (Rich reconfigures the child's stdout); decode as
    # UTF-8 here rather than the Windows default (cp1252), which would drop the
    # whole capture on the first non-cp1252 byte.
    full_env = {**os.environ, "NO_COLOR": "1", "COLUMNS": "120"}
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            argv,
            cwd=str(REPO),
            env=full_env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        output = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        output, rc = f"(timed out after {timeout}s)", 124
    dt = time.monotonic() - t0

    low = output.lower()
    # a lab step blocked by a missing extra / unreachable / unauthed model is a
    # setup boundary, not a failure
    _BOUNDARY = (
        "needs the 'lab' extra",
        "apiconnectionerror",
        "connection error",
        "could not connect",
        "connection refused",
        "authenticationerror",
    )
    if step.live and any(s in low for s in _BOUNDARY):
        return Result(step, argv, output, dt, rc, "BOUNDARY", [])
    checks = [(e, e.lower() in low) for e in step.expect]
    status = "PASS" if all(p for _, p in checks) else "FAIL"
    return Result(step, argv, output, dt, rc, status, checks)


# --------------------------------------------------------------------------- #
# presentation + logging
# --------------------------------------------------------------------------- #
def _console(no_color: bool = False):
    from rich.console import Console

    return Console(no_color=no_color, highlight=False)


def _type_out(console: Any, prompt: str, cmd: str, speed: float) -> None:
    if speed <= 0:
        console.print(f"[bold green]{prompt}[/bold green] {cmd}")
        return
    console.print(f"[bold green]{prompt}[/bold green] ", end="")
    for ch in cmd:
        console.print(ch, end="")
        console.file.flush()
        time.sleep(0.012 * speed)
    console.print()


def _agentic_notice(console: Any) -> None:
    from rich.panel import Panel

    console.print(
        Panel(
            "This step needs a model (agentic mode). To enable it:\n"
            "  1) uv sync --extra lab\n"
            "  2) a model:  ollama pull qwen2.5-coder:7b   (free, local)\n"
            "               or set ANTHROPIC_API_KEY / OPENAI_API_KEY\n"
            "Then re-run with  --live.",
            title="[bold]set up agentic mode[/bold]",
            border_style="yellow",
        )
    )


_STATUS_STYLE = {"PASS": "green", "FAIL": "bold red", "BOUNDARY": "yellow"}


def run(scenarios: list[Scenario], *, demo: bool, live: bool, speed: float) -> int:
    console = _console()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    jsonl = (LOG_DIR / f"blackbox_{stamp}.jsonl").open("w", encoding="utf-8")
    md_lines = [
        f"# datacli black-box run — {datetime.now().isoformat(timespec='seconds')}",
        "",
    ]

    totals = {"PASS": 0, "FAIL": 0, "BOUNDARY": 0, "SKIP": 0}
    for scn in scenarios:
        console.rule(f"[bold]{scn.key} · {scn.title}[/bold]  —  {scn.intent}")
        md_lines += [f"## {scn.key} · {scn.title}", f"_{scn.intent}_", ""]
        for step in scn.steps:
            if step.live and not live:
                totals["SKIP"] += 1
                if demo:
                    # In a demo, don't silently skip the agentic steps -- show the
                    # command and how to enable it, so the walkthrough is complete.
                    if step.note:
                        console.print(f"[dim]# {step.note}[/dim]")
                    _type_out(console, "eodhd>", step.cmd, speed)
                    _agentic_notice(console)
                else:
                    console.print(f"[dim]skip (live): {step.cmd}[/dim]")
                md_lines += [f"- _skipped (live): `{step.cmd}`_", ""]
                continue
            if demo and step.note:
                console.print(f"[dim]# {step.note}[/dim]")
            _type_out(console, "eodhd>", step.cmd, speed if demo else 0.0)
            res = run_step(step)
            if demo:
                console.print(res.output.rstrip("\n"))
                time.sleep(0.5 * speed)
            style = _STATUS_STYLE[res.status]
            miss = [e for e, ok in res.checks if not ok]
            detail = f"  missing: {miss}" if miss else ""
            console.print(
                f"[{style}]{res.status}[/{style}] "
                f"[dim]{res.duration_s:.1f}s · rc={res.returncode}[/dim]{detail}"
            )
            totals[res.status] += 1
            jsonl.write(
                json.dumps(
                    {
                        "scenario": scn.key,
                        "cmd": step.cmd,
                        "argv": res.argv,
                        "status": res.status,
                        "returncode": res.returncode,
                        "duration_s": round(res.duration_s, 2),
                        "checks": [{"expect": e, "passed": ok} for e, ok in res.checks],
                        "output": res.output,
                    }
                )
                + "\n"
            )
            md_lines += [
                f"### `{step.cmd}` — {res.status}",
                "",
                "```",
                res.output.rstrip("\n"),
                "```",
                "",
            ]
    jsonl.close()
    (LOG_DIR / f"blackbox_{stamp}.md").write_text("\n".join(md_lines), encoding="utf-8")

    console.rule("[bold]summary[/bold]")
    console.print(
        f"[green]{totals['PASS']} pass[/green] · [bold red]{totals['FAIL']} fail[/bold red] · "
        f"[yellow]{totals['BOUNDARY']} boundary[/yellow] · [dim]{totals['SKIP']} skipped[/dim]"
    )
    console.print(f"[dim]log: {LOG_DIR / f'blackbox_{stamp}.md'}[/dim]")
    return 1 if totals["FAIL"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="datacli black-box scenario harness")
    parser.add_argument("--demo", action="store_true", help="slow-motion, shell-styled")
    parser.add_argument(
        "--check", action="store_true", help="assert + exit code (default)"
    )
    parser.add_argument(
        "--live", action="store_true", help="include LLM/paid scenarios"
    )
    parser.add_argument(
        "--speed", type=float, default=1.0, help="demo pacing multiplier"
    )
    parser.add_argument(
        "--only", default="", help="comma list of scenario keys, e.g. S1,S2"
    )
    args = parser.parse_args(argv)

    scenarios = SCENARIOS
    if args.only:
        keys = {k.strip().upper() for k in args.only.split(",") if k.strip()}
        scenarios = [s for s in SCENARIOS if s.key in keys]
    return run(scenarios, demo=args.demo, live=args.live, speed=args.speed)


if __name__ == "__main__":
    sys.exit(main())
