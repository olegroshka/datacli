from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("cmd2")  # datacli needs cmd2 + rich

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _REPO_ROOT
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import datacli  # type: ignore  # noqa: E402


def test_eodhd_plugin_command_map() -> None:
    plugin = datacli.EodhdPlugin()
    # /fetch maps to the eodhd CLI `refresh` subcommand; args pass through.
    assert plugin.build_argv("fetch", ["--fast", "--run"]) == [
        "refresh",
        "--fast",
        "--run",
    ]
    assert plugin.build_argv("status", []) == ["status"]
    assert plugin.build_argv("qc", ["--lane", "us_common"]) == [
        "qc",
        "--lane",
        "us_common",
    ]
    assert plugin.build_argv("lanes", []) == ["lanes"]


def test_eodhd_plugin_command_names() -> None:
    names = datacli.EodhdPlugin().command_names()
    assert set(names) == {"status", "fetch", "qc", "lanes", "probe", "config"}


def test_sources_registry() -> None:
    assert "eodhd" in datacli.SOURCES
    # fred/yahoo are load-only adapters (operational plugins deferred).
    assert "fred" not in datacli.SOURCES
    assert "fred" in datacli.LOAD_ONLY


def test_argv_parses_string_and_arg_list() -> None:
    assert datacli.DataCli._argv("--fast --run") == ["--fast", "--run"]

    class _Stmt:
        arg_list = ["--lane", "us_etf"]

    assert datacli.DataCli._argv(_Stmt()) == ["--lane", "us_etf"]
