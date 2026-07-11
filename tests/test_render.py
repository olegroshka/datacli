from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import _render  # type: ignore  # noqa: E402


def test_fmt_compact() -> None:
    assert _render.fmt_compact(12_660_764) == "12.66M"
    assert _render.fmt_compact(168_714) == "168.7K"
    assert _render.fmt_compact(227) == "227"
    assert _render.fmt_compact(0) == "0"
    assert _render.fmt_compact(None) == "-"


def test_fmt_int() -> None:
    assert _render.fmt_int(2595) == "2,595"
    assert _render.fmt_int(None) == "-"


def test_freshness_style_gradient() -> None:
    assert _render.freshness_style(None) == "dim"
    assert _render.freshness_style(1) == "green"
    assert _render.freshness_style(5) == ""  # in-window: quiet
    assert _render.freshness_style(20) == "yellow"
    assert _render.freshness_style(40) == "red"


def test_flag_and_severity_cells_carry_glyph_and_text() -> None:
    ok = _render.flag_cell("ok")
    assert "✓" in ok.plain and "ok" in ok.plain
    stale = _render.flag_cell("STALE")
    assert "⚠" in stale.plain
    assert "✗" in _render.severity_cell("error").plain
    assert "⚠" in _render.severity_cell("warning").plain


def test_action_cell_colours_by_cost() -> None:
    assert _render.action_cell("full_refresh").style == "red"
    assert _render.action_cell("targeted_rerun").style == "yellow"
    assert _render.action_cell("-").style == "dim"


def test_kind_dot() -> None:
    assert _render.kind_dot("prices").plain == _render.KIND_DOT
    assert _render.kind_dot("prices").style == "cyan"
    # unknown kind -> blank placeholder, no colour
    assert _render.kind_dot("weather").plain == " "


def test_state_cell() -> None:
    text = _render.state_cell({"ok": 2593, "empty": 2})
    assert "ok" in text.plain and "2,593" in text.plain and "empty" in text.plain
    # non-mapping (snapshot label) renders dim as-is
    assert _render.state_cell("snapshot").plain == "snapshot"
    assert _render.state_cell({}).plain == "-"


def test_counts_cell_dims_zero() -> None:
    zero = _render.counts_cell({"error": 0}, key="error", style="red")
    assert zero.plain == "0" and zero.style == "dim"
    hot = _render.counts_cell({"error": 7}, key="error", style="red")
    assert hot.plain == "7" and hot.style == "red"


def test_make_console_no_color() -> None:
    con = _render.make_console(no_color=True)
    assert con.no_color is True
