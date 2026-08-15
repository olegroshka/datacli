"""Shared Rich rendering helpers for the eodhd console reports (status, qc).

Design rules for every colour used here:

- **Colour encodes meaning, never decoration.** Each hue maps to a semantic
  (dataset kind, severity, freshness, action cost). Healthy rows stay quiet so
  anomalies pop.
- **Colour + glyph, never colour alone.** Every coloured state is paired with a
  symbol (``✓ ⚠ ✗ ●``) so the output survives ``NO_COLOR``, piping to a file, and
  colour-blindness. Rich drops the colour automatically off a TTY; the glyph and
  layout still carry the signal.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Mapping

import pandas as pd
from rich import box
from rich.console import Console
from rich.table import Table
from rich.text import Text

# --------------------------------------------------------------------------- #
# semantic palettes
# --------------------------------------------------------------------------- #
# dataset-kind hue -- a small key you learn once and read everywhere.
KIND_HUE: dict[str, str] = {
    "prices": "cyan",
    "dividends": "green",
    "splits": "magenta",
    "fundamentals": "yellow",
    "news": "blue",
}
KIND_DOT = "●"  # ●

# severity -> (glyph, style)
SEVERITY: dict[str, tuple[str, str]] = {
    "error": ("✗", "bold red"),  # ✗
    "warning": ("⚠", "yellow"),  # ⚠
    "ok": ("✓", "green"),  # ✓
    "info": ("•", "cyan"),  # •
}

# health flag (status report) -> (glyph, style)
FLAG: dict[str, tuple[str, str]] = {
    "ok": ("✓", "green"),  # ✓
    "STALE": ("⚠", "yellow"),  # ⚠
    "ABSENT": ("✗", "bold red"),  # ✗
    "-": ("·", "dim"),  # ·
}

# recommendation cost -- louder = more expensive to run.
ACTION_STYLE: dict[str, str] = {
    "full_refresh": "red",
    "targeted_rerun": "yellow",
    "incremental_rerun": "yellow",
    "-": "dim",
}

# per-token colours inside a state breakdown (ok=2593 empty=2 ...).
STATE_TOKEN_STYLE: dict[str, str] = {
    "ok": "green",
    "up_to_date": "cyan",
    "empty": "dim",
    "error": "red",
    "missing": "red",
}

DIVIDER = "·"  # · used between compact tokens


# --------------------------------------------------------------------------- #
# console
# --------------------------------------------------------------------------- #
def make_console(*, no_color: bool | None = None, force_color: bool = False) -> Console:
    """A Console honouring ``NO_COLOR`` / ``FORCE_COLOR`` and TTY detection.

    Args:
        no_color: Force monochrome. ``None`` defers to the ``NO_COLOR`` env var
            (and Rich's own non-TTY detection).
        force_color: Emit ANSI even when stdout is not a TTY (e.g. captured by a
            host shell). ``FORCE_COLOR`` in the environment does the same.
    """
    if no_color is None:
        no_color = bool(os.environ.get("NO_COLOR"))
    force = force_color or bool(os.environ.get("FORCE_COLOR"))
    # Box-drawing and glyphs (●, ─, ✓) are outside cp1252; make sure the stream
    # can encode them on legacy Windows consoles. Harmless elsewhere.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:
        pass
    kwargs: dict[str, Any] = {"highlight": False, "no_color": no_color}
    if force:
        # Skip the win32 legacy renderer (it re-encodes to cp1252 and chokes on
        # the glyphs); emit modern ANSI to the reconfigured UTF-8 stream instead.
        kwargs["force_terminal"] = True
        kwargs["legacy_windows"] = False
    return Console(**kwargs)


# --------------------------------------------------------------------------- #
# tables
# --------------------------------------------------------------------------- #
def minimal_table(
    *, title: str | None = None, caption: str | None = None, **kwargs: Any
) -> Table:
    """A light table: a single rule under the header, no vertical borders.

    The right shape for wide/dense dashboards where heavy Unicode boxes wrap and
    add noise; colour and alignment do the structural work instead.
    """
    return Table(
        box=box.SIMPLE_HEAD,
        title=title,
        title_style="bold",
        caption=caption,
        caption_style="dim",
        header_style="bold",
        pad_edge=False,
        expand=False,
        **kwargs,
    )


def boxed_table(
    *, title: str | None = None, caption: str | None = None, **kwargs: Any
) -> Table:
    """A framed table (heavy header rule + light grid) for narrow reference views.

    The heavier sibling of :func:`minimal_table`; shares the same header/title
    styling so the two read as one family. Right for small, look-up-style tables
    (sources, config, one ticker's datasets) rather than wide dashboards.
    """
    return Table(
        box=box.HEAVY_HEAD,
        title=title,
        title_style="bold",
        caption=caption,
        caption_style="dim",
        header_style="bold",
        **kwargs,
    )


def yesno_cell(present: bool) -> Text:
    """``✓ yes`` (green) / ``· no`` (dim) — presence with a glyph, not colour alone."""
    return Text("✓ yes", style="green") if present else Text("· no", style="dim")


def _fmt_cell(value: Any) -> str:
    """Format one DataFrame value: ``-`` for NA, grouped ints, tidy floats."""
    if pd.isna(value):
        return "-"
    if isinstance(value, bool):
        return str(value)
    if pd.api.types.is_integer(value):
        return f"{int(value):,}"
    if pd.api.types.is_float(value):
        f = float(value)
        return f"{f:,.2f}" if abs(f) >= 1_000_000 else f"{f:.6g}"
    return str(value)


def df_table(
    df: pd.DataFrame, *, title: str | None = None, caption: str | None = None
) -> Table:
    """Render a DataFrame as a minimal Rich table (numeric columns right-aligned).

    Values are formatted losslessly-enough for display (grouped integers, ``%g``
    floats) so ad-hoc ``rows``/``sql``/``probe`` output looks like the rest of the
    tool instead of a raw pandas dump.
    """
    table = minimal_table(title=title, caption=caption)
    for col in df.columns:
        is_num = pd.api.types.is_numeric_dtype(
            df[col]
        ) and not pd.api.types.is_bool_dtype(df[col])
        table.add_column(str(col), justify="right" if is_num else "left", no_wrap=True)
    for _, row in df.iterrows():
        table.add_row(*[_fmt_cell(row[col]) for col in df.columns])
    return table


# --------------------------------------------------------------------------- #
# cell builders (return Rich Text so colour degrades to plain cleanly)
# --------------------------------------------------------------------------- #
def kind_dot(kind: str) -> Text:
    """A ``●`` dot in the dataset-kind hue (blank for unknown kinds)."""
    hue = KIND_HUE.get(kind)
    return Text(KIND_DOT if hue else " ", style=hue or "")


def severity_cell(severity: str) -> Text:
    glyph, style = SEVERITY.get(severity, ("•", "dim"))
    return Text(glyph, style=style)


def flag_cell(flag: str) -> Text:
    glyph, style = FLAG.get(flag, ("·", "dim"))
    return Text(f"{glyph} {flag}", style=style)


def action_cell(recommendation: str) -> Text:
    style = ACTION_STYLE.get(recommendation, "dim")
    return Text(recommendation or "-", style=style)


def freshness_style(age_days: int | None) -> str:
    """Staleness gradient: fresh green, ageing default, stale yellow, old red."""
    if age_days is None:
        return "dim"
    if age_days <= 2:
        return "green"
    if age_days <= 7:
        return ""  # in-window: leave default (quiet)
    if age_days <= 30:
        return "yellow"
    return "red"


def age_cell(age_days: int | None) -> Text:
    if age_days is None:
        return Text("-", style="dim")
    return Text(f"{age_days}d", style=freshness_style(age_days))


def state_cell(counts: Mapping[str, Any] | str) -> Text:
    """A compact, per-token-coloured state breakdown: ``ok 2593 · empty 2``."""
    if not isinstance(counts, Mapping):
        return Text(str(counts), style="dim")
    if not counts:
        return Text("-", style="dim")
    text = Text()
    for i, (name, value) in enumerate(counts.items()):
        if i:
            text.append(f" {DIVIDER} ", style="dim")
        style = STATE_TOKEN_STYLE.get(name, "yellow")
        text.append(f"{name} ", style=style)
        text.append(f"{value:,}" if isinstance(value, int) else str(value), style=style)
    return text


def status_token(status: str | None) -> Text:
    """Colour a single fetch-status token (``ok`` green, ``empty`` dim, ...)."""
    if not status or status == "-":
        return Text("-", style="dim")
    return Text(status, style=STATE_TOKEN_STYLE.get(status, "yellow"))


def counts_cell(counts: Mapping[str, int] | None, *, key: str, style: str) -> Text:
    """A single severity count (err/warn), dimmed when zero."""
    value = (counts or {}).get(key, 0)
    if not value:
        return Text("0", style="dim")
    return Text(str(value), style=style)


# --------------------------------------------------------------------------- #
# number / date formatting
# --------------------------------------------------------------------------- #
def fmt_compact(value: int | float | None) -> str:
    """Human-compact magnitude: ``12.66M`` / ``168.7K`` / ``227``."""
    if value is None:
        return "-"
    n = int(value)
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.1f}K"
    return f"{n:,}"


def fmt_int(value: int | None) -> str:
    return "-" if value is None else f"{value:,}"
