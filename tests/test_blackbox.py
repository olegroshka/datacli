from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import blackbox  # type: ignore  # noqa: E402


def test_routing_maps_commands_to_entry_points() -> None:
    py = sys.executable
    assert blackbox._full_argv("status us_common") == [
        py,
        "eodhd/cli.py",
        "status",
        "us_common",
    ]
    assert blackbox._full_argv("describe VAR.OL") == [
        py,
        "eodhd/cli.py",
        "describe",
        "VAR.OL",
    ]
    assert blackbox._full_argv("macro list") == [py, "-m", "macro.cli", "list"]
    assert blackbox._full_argv("lab config") == [py, "-m", "lab.cli", "config"]
    # top-level lab verbs route to the lab CLI with the verb kept
    assert blackbox._full_argv('ask "q"') == [py, "-m", "lab.cli", "ask", "q"]
    assert blackbox._full_argv("investigate topic") == [
        py,
        "-m",
        "lab.cli",
        "investigate",
        "topic",
    ]


def test_unknown_command_raises() -> None:
    import pytest

    with pytest.raises(ValueError):
        blackbox._full_argv("frobnicate x")


def test_scenarios_are_well_formed() -> None:
    keys = [s.key for s in blackbox.SCENARIOS]
    assert keys == ["S1", "S2", "S3", "S4", "S5"]
    for scn in blackbox.SCENARIOS:
        assert scn.steps, f"{scn.key} has no steps"
        for step in scn.steps:
            assert step.cmd and isinstance(step.expect, tuple)
    # the LLM scenarios are gated behind --live
    live_keys = {s.key for s in blackbox.SCENARIOS if any(st.live for st in s.steps)}
    assert {"S4", "S5"} <= live_keys
