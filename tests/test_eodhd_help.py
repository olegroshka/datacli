"""``--help`` surfaces of the scripts ``eodhd/cli.py`` delegates to.

``cli.py`` exports ``DATACLI_PROG`` (e.g. ``"eodhd qc"``) when it forwards to a
standalone script so the usage line reads as the command the user typed rather
than the script filename. These tests pin that contract plus the wording fixes
from the help review (no network, no data root needed).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import probe_eodhd_availability as probe  # type: ignore  # noqa: E402
import report_eodhd_raw_quality as qc  # type: ignore  # noqa: E402


def test_qc_help_uses_datacli_prog(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setenv("DATACLI_PROG", "eodhd qc")
    with pytest.raises(SystemExit) as exc:
        qc.parse_args(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("usage: eodhd qc")
    assert "price-bearing lanes" in out
    assert "ETF and index-reference" not in out
    # --lane carries a help string now
    lane_idx = out.index("--lane")
    assert "Audit one price-bearing lane" in out[lane_idx:]


def test_qc_help_falls_back_to_script_name(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("DATACLI_PROG", raising=False)
    monkeypatch.setattr(sys, "argv", ["report_eodhd_raw_quality.py", "--help"])
    with pytest.raises(SystemExit):
        qc.parse_args(["--help"])
    assert capsys.readouterr().out.startswith("usage: report_eodhd_raw_quality.py")


def test_probe_help_states_paid_api_and_cache(
    monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setenv("DATACLI_PROG", "eodhd probe")
    monkeypatch.setattr(sys, "argv", ["probe_eodhd_availability.py", "--help"])
    with pytest.raises(SystemExit) as exc:
        probe.parse_args()
    assert exc.value.code == 0
    out = " ".join(capsys.readouterr().out.split())
    assert out.startswith("usage: eodhd probe")
    assert "paid EODHD API" in out
    assert "probe_cache" in out
    assert "never touches the lane outputs" in out
