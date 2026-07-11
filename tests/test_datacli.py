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
    assert set(names) == {
        "status",
        "fetch",
        "qc",
        "lanes",
        "probe",
        "config",
        "describe",
        "find",
        "rows",
        "coverage",
        "sql",
        "schema",
        "reindex",
    }


def test_sources_registry() -> None:
    assert "eodhd" in datacli.SOURCES
    # fred/yahoo are load-only adapters (operational plugins deferred).
    assert "fred" not in datacli.SOURCES
    assert "fred" in datacli.LOAD_ONLY


def test_clear_command_exists_and_runs() -> None:
    app = datacli.DataCli()
    assert hasattr(app, "do_clear")
    app.do_clear("")  # must not raise off a TTY


def test_exit_command_signals_stop() -> None:
    app = datacli.DataCli()
    assert hasattr(app, "do_exit")
    assert app.do_exit("") is True  # returning True ends the cmd2 loop


def _completions(comps: object) -> set[str]:
    return {item.text for item in comps}  # type: ignore[attr-defined]


def test_slash_is_optional_for_execution_and_completion() -> None:
    app = datacli.DataCli()
    # the '/' shortcut resolves both forms to the same command
    assert app.statement_parser.parse("/qc us_common").command == "qc"
    assert app.statement_parser.parse("qc us_common").command == "qc"


def test_complete_qc_lane_then_dataset() -> None:
    app = datacli.DataCli()
    # first positional -> lanes (prefix filtered)
    assert _completions(app.complete_qc("us_", "qc us_", 3, 6)) == {
        "us_common",
        "us_etf",
    }
    # second positional -> datasets
    assert _completions(app.complete_qc("di", "qc us_common di", 13, 15)) == {
        "dividends"
    }


def test_complete_routes_through_slash() -> None:
    app = datacli.DataCli()
    # '/qc us_<tab>' must reach the qc completer via the '/' shortcut
    assert _completions(app.complete("us_", "/qc us_", 4, 7)) == {
        "us_common",
        "us_etf",
    }


def test_complete_status_rows_config_source() -> None:
    app = datacli.DataCli()
    assert "uk_eu" in _completions(app.complete_status("uk", "status uk", 7, 9))
    assert _completions(app.complete_rows("f", "rows VAR.OL f", 12, 13)) == {
        "fundamentals"
    }
    assert _completions(app.complete_config("s", "config s", 7, 8)) == {"set", "show"}
    assert "eodhd" in _completions(app.complete_source("e", "source e", 7, 8))


def test_argv_parses_string_and_arg_list() -> None:
    assert datacli.DataCli._argv("--fast --run") == ["--fast", "--run"]

    class _Stmt:
        arg_list = ["--lane", "us_etf"]

    assert datacli.DataCli._argv(_Stmt()) == ["--lane", "us_etf"]
