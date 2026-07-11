"""``lab`` command group -- the front door to the Raw Data Lab.

Phase 0 ships ``lab config``; ``ask`` / ``agent`` / ``lab agents`` / ``lab skills``
arrive in Phase 1. Rendering reuses the shared eodhd ``_render`` palette so the lab
looks like the rest of the tool.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# allow `python lab/cli.py` as well as `python -m lab.cli` / shell import
_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lab import config as lab_config  # noqa: E402

_EODHD = _REPO / "eodhd"
if str(_EODHD) not in sys.path:
    sys.path.insert(0, str(_EODHD))
import _render  # type: ignore[import-not-found]  # noqa: E402

PROG = "lab"

# Provider env vars we surface in `lab config` (presence only, always masked).
_PROVIDER_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
}


def _mask(value: str) -> str:
    return ("****" + value[-4:]) if len(value) >= 4 else "set"


def _litellm_installed() -> bool:
    import importlib.util

    return importlib.util.find_spec("litellm") is not None


def cmd_config(argv: list[str]) -> int:
    """Show the resolved lab configuration (models, budget, cache, providers)."""
    from rich.text import Text

    cfg = lab_config.load()
    console = _render.make_console()

    # -- settings ------------------------------------------------------------ #
    table = _render.boxed_table(title="datacli lab config")
    table.add_column("setting", style="cyan", no_wrap=True)
    table.add_column("value")

    table.add_row("default persona", cfg.default_persona)
    budget = (
        "unbounded"
        if cfg.budget.per_session_usd is None
        else f"${cfg.budget.per_session_usd:.2f} / session"
    )
    if cfg.budget.warn_usd is not None:
        budget += f"  (warn at ${cfg.budget.warn_usd:.2f})"
    table.add_row("budget", budget)
    cache_exists = cfg.cache_dir.exists()
    cache_val = Text(str(cfg.cache_dir))
    cache_val.append(
        f"  ({cache_mod_count(cfg)} cached)" if cache_exists else "  (empty)",
        style="dim",
    )
    table.add_row("cache", cache_val)
    engine = Text("litellm", style="green" if _litellm_installed() else "red")
    if not _litellm_installed():
        engine.append("  — not installed (uv sync --extra lab)", style="dim")
    table.add_row("engine", engine)
    console.print(table)

    # -- model tiers --------------------------------------------------------- #
    tiers = _render.boxed_table(title="model tiers")
    tiers.add_column("tier", style="cyan", no_wrap=True)
    tiers.add_column("model")
    for tier, model in cfg.models.items():
        provider = model.split("/", 1)[0] if "/" in model else "?"
        row = Text(model)
        row.append(f"  [{provider}]", style="dim")
        tiers.add_row(tier, row)
    console.print(tiers)

    # -- providers ----------------------------------------------------------- #
    prov = _render.boxed_table(
        title="providers", caption="keys come from the environment; never stored"
    )
    prov.add_column("provider", style="cyan", no_wrap=True)
    prov.add_column("status")
    for name, env in _PROVIDER_KEYS.items():
        value = os.environ.get(env)
        cell = (
            Text(_mask(value), style="green") if value else Text("not set", style="dim")
        )
        prov.add_row(name, cell)
    host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    prov.add_row("ollama (local)", Text(host, style="dim"))
    console.print(prov)
    return 0


def cache_mod_count(cfg: lab_config.LabConfig) -> int:
    from lab.cache import ResponseCache

    return ResponseCache(cfg.cache_dir).count()


def top_help() -> str:
    return (
        "lab -- Raw Data Lab (grounded EDA copilot)\n\n"
        "Commands:\n"
        "  config    Show models, budget, cache and provider status\n\n"
        "  (ask / agent / lab agents / lab skills arrive in Phase 1)\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_config([])
    command, rest = args[0], args[1:]
    if command in ("-h", "--help", "help"):
        print(top_help())
        return 0
    if command == "config":
        return cmd_config(rest)
    print(f"unknown lab command: {command!r}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
