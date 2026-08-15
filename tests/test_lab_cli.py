"""``lab`` CLI: help / flag parsing must never reach a model.

Every model-backed entry point is monkeypatched to raise, so a regression that
runs the agent on ``--help`` or on a mistyped flag fails loudly here instead of
spending budget.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lab import cli as lab_cli  # noqa: E402


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("a model / data entry point was touched")


@pytest.fixture(autouse=True)
def _no_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything past flag parsing runs."""
    from lab import agent as lab_agent
    from lab import models as lab_models
    from lab import pipeline as lab_pipeline

    monkeypatch.setattr(lab_cli, "_agent_context", _boom)
    monkeypatch.setattr(lab_agent, "run", _boom)
    monkeypatch.setattr(lab_pipeline, "investigate", _boom)
    monkeypatch.setattr(lab_models.LLM, "__init__", _boom)


# --------------------------------------------------------------------------- #
# --help for every command exits 0 without touching a model
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("command", sorted(lab_cli.COMMANDS))
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_per_command(
    command: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lab_cli.main([command, flag]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: {lab_cli.COMMANDS[command].usage}")
    assert "-h, --help" in out


@pytest.mark.parametrize(
    "argv",
    [
        ["ask", "--help", "what is the worst coverage?"],  # help wins over the question
        ["ask", "worst coverage?", "--help"],
        ["run", "coverage-audit", "--help"],  # before the skill is even loaded
        ["agent", "auditor", "check splits", "-h"],
        ["investigate", "--generator", "skeptic", "topic", "--help"],
    ],
)
def test_help_beats_model_work(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert lab_cli.main(argv) == 0
    assert capsys.readouterr().out.startswith("usage: ")


def test_top_help_and_help_subcommand(capsys: pytest.CaptureFixture[str]) -> None:
    assert lab_cli.main(["--help"]) == 0
    top = capsys.readouterr().out
    assert "bare `lab` == `lab config`" in top
    for cmd in lab_cli.COMMANDS.values():
        assert cmd.synopsis in top  # one source of truth
    assert lab_cli.main(["help", "ask"]) == 0
    assert capsys.readouterr().out.startswith("usage: ask <question>")


def test_usage_strings_are_consistent() -> None:
    assert (
        lab_cli.COMMANDS["run"].usage
        == "lab run <skill> [args] [--verify] [--no-report]"
    )
    assert lab_cli.COMMANDS["ask"].usage == "ask <question> [--verify] [--report]"
    assert (
        lab_cli.COMMANDS["investigate"].usage
        == "investigate <topic> [--generator <persona>] [--no-report]"
    )
    # per-command help and the top help are rendered from the same table
    for name, cmd in lab_cli.COMMANDS.items():
        assert lab_cli.command_help(name).splitlines()[0] == f"usage: {cmd.usage}"


# --------------------------------------------------------------------------- #
# unknown / malformed flags are rejected (exit 2) with a did-you-mean hint
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["ask", "--repor", "q"], "did you mean --report"),
        (["ask", "q", "--verfy"], "did you mean --verify"),
        (["run", "coverage-audit", "--no-reprot"], "did you mean --no-report"),
        (["investigate", "t", "--generatr=skeptic"], "did you mean --generator"),
        (["investigate", "--generator"], "needs a value"),
        (["investigate", "--generator", "--no-report", "t"], "needs a value"),
        (["ask", "--verify=yes", "q"], "does not take a value"),
        (["config", "--bogus"], "unknown flag --bogus"),
        (["agents", "--all"], "unknown flag --all"),
        (["skills", "--foo"], "unknown flag --foo"),
    ],
)
def test_bad_flags_exit_2(
    argv: list[str], expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert lab_cli.main(argv) == 2
    out = capsys.readouterr().out
    assert expect in out
    assert "usage: " in out


def test_unknown_command_hints(capsys: pytest.CaptureFixture[str]) -> None:
    assert lab_cli.main(["confg"]) == 2
    captured = capsys.readouterr()
    assert "did you mean config" in captured.err
    assert "Commands:" in captured.out


# --------------------------------------------------------------------------- #
# successful parses hand the same arguments to the runners as before
# --------------------------------------------------------------------------- #
def test_generator_both_spellings(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(
        lab_cli,
        "_run_pipeline",
        lambda topic, gen, *, report: calls.append((topic, gen, report)) or 0,
    )
    assert lab_cli.main(["investigate", "--generator", "skeptic", "t"]) == 0
    assert lab_cli.main(["investigate", "t", "--generator=skeptic"]) == 0
    assert lab_cli.main(["investigate", "--generator=quant", "--no-report", "a b"]) == 0
    assert calls == [
        ("t", "skeptic", True),
        ("t", "skeptic", True),
        ("a b", "quant", False),
    ]


def test_ask_and_agent_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def fake_run_agent(persona: str, question: str, **kw: object) -> int:
        calls.append((persona, question, kw.get("verify"), kw.get("report")))
        return 0

    monkeypatch.setattr(lab_cli, "_run_agent", fake_run_agent)
    monkeypatch.setattr(
        lab_cli.lab_config, "load", lambda *a, **k: lab_cli.lab_config.LabConfig()
    )
    assert lab_cli.main(["ask", "worst", "coverage?", "--verify"]) == 0
    assert lab_cli.main(["agent", "auditor", "--report", "check", "splits"]) == 0
    assert lab_cli.main(["ask", "--", "--not-a-flag"]) == 0  # `--` ends flags
    assert calls == [
        ("analyst", "worst coverage?", True, False),
        ("auditor", "check splits", False, True),
        ("analyst", "--not-a-flag", False, False),
    ]


def test_run_passes_skill_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    def fake_run_agent(persona: str, task: str, **kw: object) -> int:
        calls.append((task, kw.get("verify"), kw.get("report"), kw.get("title")))
        return 0

    monkeypatch.setattr(lab_cli, "_run_agent", fake_run_agent)
    assert lab_cli.main(["run", "coverage-audit", "us_common", "--verify"]) == 0
    ((task, verify, report, title),) = calls
    assert "Inputs provided: us_common" in task
    assert (verify, report, title) == (True, True, "coverage-audit")
    assert lab_cli.main(["run", "coverage-audit", "--no-report"]) == 0
    assert calls[-1][2] is False


def test_agent_and_run_without_positionals(capsys: pytest.CaptureFixture[str]) -> None:
    assert lab_cli.main(["agent"]) == 2
    assert "usage: agent <persona> <task>" in capsys.readouterr().out
    assert lab_cli.main(["run"]) == 2
    assert "usage: lab run <skill>" in capsys.readouterr().out
