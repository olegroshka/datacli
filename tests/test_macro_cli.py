"""``macro`` CLI: help / flag parsing must never reach the network or the disk."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from macro import cli as macro_cli  # noqa: E402
from macro import eodhd, fred  # noqa: E402


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("a fetch / load entry point was touched")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if a real fetch (or, for help paths, a disk load) runs."""
    monkeypatch.setattr(fred, "refresh", _boom)
    monkeypatch.setattr(eodhd, "refresh", _boom)
    monkeypatch.setattr(eodhd, "refresh_market", _boom)


@pytest.fixture
def _no_disk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fred, "load", _boom)
    monkeypatch.setattr(eodhd, "load", _boom)
    monkeypatch.setattr(eodhd, "load_market", _boom)


# --------------------------------------------------------------------------- #
# --help per subcommand
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("_no_disk")
@pytest.mark.parametrize("command", sorted(macro_cli.COMMANDS))
@pytest.mark.parametrize("flag", ["--help", "-h"])
def test_help_per_command(
    command: str, flag: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert macro_cli.main([command, flag]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"usage: {macro_cli.COMMANDS[command].usage}")
    assert "-h, --help" in out


def test_fetch_run_help_does_not_fetch(capsys: pytest.CaptureFixture[str]) -> None:
    assert macro_cli.main(["fetch", "--run", "--help"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: macro fetch")
    assert "FRED_API_KEY" in out and "EODHD_API_KEY" in out
    assert "dry run" in out


def test_top_help_documents_flags_and_keys(capsys: pytest.CaptureFixture[str]) -> None:
    assert macro_cli.main(["--help"]) == 0
    out = capsys.readouterr().out
    assert "bare `macro` == `macro status`" in out
    for flag in ("--provider", "--run", "--full"):
        assert flag in out
    assert "FRED_API_KEY" in out and "EODHD_API_KEY" in out
    for cmd in macro_cli.COMMANDS.values():
        assert cmd.synopsis in out
    assert macro_cli.main(["help", "fetch"]) == 0
    assert capsys.readouterr().out.startswith("usage: macro fetch")


# --------------------------------------------------------------------------- #
# unknown flags / providers are rejected (exit 2) with a hint
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("argv", "expect"),
    [
        (["fetch", "--provder", "fred"], "did you mean --provider"),
        (["fetch", "--ful"], "did you mean --full"),
        (["fetch", "--rnu"], "did you mean --run"),
        (["fetch", "--provider"], "needs a value"),
        (["fetch", "--run=yes"], "does not take a value"),
        (["status", "--verbose"], "unknown flag --verbose"),
        (["list", "--all"], "unknown flag --all"),
    ],
)
def test_bad_flags_exit_2(
    argv: list[str], expect: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert macro_cli.main(argv) == 2
    out = capsys.readouterr().out
    assert expect in out and "usage: macro" in out


def test_bad_provider_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert macro_cli.main(["fetch", "--provider", "frd"]) == 2
    out = capsys.readouterr().out
    assert "unknown provider 'frd'" in out and "did you mean fred" in out
    assert macro_cli.main(["fetch", "--provider=eodh", "--run"]) == 2  # never fetches


def test_unknown_command_hints(capsys: pytest.CaptureFixture[str]) -> None:
    assert macro_cli.main(["fetc"]) == 2
    captured = capsys.readouterr()
    assert "did you mean fetch" in captured.err


# --------------------------------------------------------------------------- #
# the dry-run plan works without keys and never fetches
# --------------------------------------------------------------------------- #
def test_fetch_dry_run_without_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    monkeypatch.setattr(eodhd, "api_key", lambda: None)
    assert macro_cli.main(["fetch"]) == 0
    out = capsys.readouterr().out
    assert "plan ->" in out and out.count("NOT SET") == 2
    assert "--run" in out  # tells you how to actually fetch


def test_fetch_dry_run_provider_both_spellings(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FRED_API_KEY", "x")
    assert macro_cli.main(["fetch", "--provider", "fred"]) == 0
    a = capsys.readouterr().out
    assert macro_cli.main(["fetch", "--provider=fred"]) == 0
    b = capsys.readouterr().out
    assert a == b and "FRED" in a and "EODHD" not in a
