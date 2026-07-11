from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import cli  # type: ignore  # noqa: E402


def test_leading_positionals_splits_before_first_flag() -> None:
    assert cli._leading_positionals([]) == ([], [])
    assert cli._leading_positionals(["us_common"]) == (["us_common"], [])
    assert cli._leading_positionals(["us_common", "splits"]) == (
        ["us_common", "splits"],
        [],
    )
    # flags and their values stay in the remainder, untouched
    assert cli._leading_positionals(["us_common", "--as-of", "2026-01-01"]) == (
        ["us_common"],
        ["--as-of", "2026-01-01"],
    )
    assert cli._leading_positionals(["--lane", "uk_eu"]) == ([], ["--lane", "uk_eu"])


def _capture(monkeypatch: object) -> list[tuple[str, list[str]]]:
    calls: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli, "delegate", lambda script, argv: calls.append((script, argv)) or 0
    )
    return calls


def test_cmd_qc_translates_lane_and_dataset(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    cli.cmd_qc(["us_common", "splits"])
    assert calls == [(cli.QC_SCRIPT, ["--lane", "us_common", "--dataset", "splits"])]


def test_cmd_qc_lane_only_and_flag_passthrough(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    cli.cmd_qc(["us_common", "--color"])
    assert calls == [(cli.QC_SCRIPT, ["--lane", "us_common", "--color"])]


def test_cmd_qc_no_positionals(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    cli.cmd_qc([])
    assert calls == [(cli.QC_SCRIPT, [])]


def test_cmd_status_translates_lane(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    cli.cmd_status(["uk_eu"])
    assert calls == [(cli.STATUS_SCRIPT, ["--lane", "uk_eu"])]


def test_cmd_qc_rejects_bad_dataset_without_delegating(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    rc = cli.cmd_qc(["us_common", "divivdends"])  # typo
    assert rc == 2
    assert calls == []  # never forwarded to the audit script


def test_cmd_qc_rejects_bad_lane(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    rc = cli.cmd_qc(["us_comon"])  # typo
    assert rc == 2
    assert calls == []


def test_cmd_status_rejects_bad_lane(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    rc = cli.cmd_status(["wibble"])
    assert rc == 2
    assert calls == []


def test_qc_all_is_a_valid_lane(monkeypatch: object) -> None:
    calls = _capture(monkeypatch)
    cli.cmd_qc(["all", "prices"])
    assert calls == [(cli.QC_SCRIPT, ["--lane", "all", "--dataset", "prices"])]
