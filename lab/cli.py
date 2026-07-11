"""``lab`` command group -- the front door to the Raw Data Lab.

``lab config`` (models / budget / cache / providers), ``lab agents`` / ``lab
skills`` (the roster), ``lab run <skill>``, and the top-level ``ask`` / ``agent``
grounded-analyst commands. Rendering reuses the shared eodhd ``_render`` palette so
the lab looks like the rest of the tool.
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


# --------------------------------------------------------------------------- #
# roster
# --------------------------------------------------------------------------- #
def cmd_agents(argv: list[str]) -> int:
    """List the configured personas."""
    from rich.text import Text

    from lab import registry

    personas = registry.load_personas()
    console = _render.make_console()
    if not personas:
        console.print("[yellow]no personas configured[/yellow] (lab/personas/*.toml)")
        return 0
    table = _render.boxed_table(title=f"lab agents ({len(personas)})")
    table.add_column("persona", style="cyan", no_wrap=True)
    table.add_column("model", no_wrap=True)
    table.add_column("description")
    for p in personas.values():
        table.add_row(p.name, Text(p.model, style="dim"), p.description)
    console.print(table)
    return 0


def cmd_skills(argv: list[str]) -> int:
    """List the configured EDA skills."""
    from lab import registry

    skills = registry.load_skills()
    console = _render.make_console()
    if not skills:
        console.print("[yellow]no skills configured[/yellow] (lab/skills/*/SKILL.md)")
        return 0
    table = _render.boxed_table(title=f"lab skills ({len(skills)})")
    table.add_column("skill", style="cyan", no_wrap=True)
    table.add_column("inputs", no_wrap=True)
    table.add_column("summary")
    for s in skills.values():
        table.add_row(s.name, ", ".join(s.inputs) or "-", s.summary)
    console.print(table)
    return 0


# --------------------------------------------------------------------------- #
# the grounded analyst
# --------------------------------------------------------------------------- #
def _render_answer(console: object, bundle: object, persona_name: str) -> None:
    from rich.panel import Panel
    from rich.syntax import Syntax
    from rich.text import Text

    con = console  # typed loosely to avoid importing Console here
    con.print(  # type: ignore[attr-defined]
        Panel(
            bundle.narrative or "(no answer)",  # type: ignore[attr-defined]
            title=f"answer · {persona_name}",
            border_style="cyan",
        )
    )
    for i, finding in enumerate(bundle.findings, 1):  # type: ignore[attr-defined]
        con.rule(f"query {i}", style="dim", align="left")  # type: ignore[attr-defined]
        con.print(Syntax(finding.sql, "sql", background_color="default", word_wrap=True))  # type: ignore[attr-defined]
        table = _render.minimal_table()
        for column in finding.columns:
            table.add_column(str(column), no_wrap=True)
        for row in finding.rows[:50]:
            table.add_row(*("NULL" if v is None else str(v) for v in row))
        con.print(table)  # type: ignore[attr-defined]
    footer = Text(
        f"{bundle.steps} step(s) · {len(bundle.findings)} query(ies) · "  # type: ignore[attr-defined]
        f"${bundle.spent_usd:.4f}",  # type: ignore[attr-defined]
        style="dim",
    )
    con.print(footer)  # type: ignore[attr-defined]


def _run_agent(persona_name: str, question: str) -> int:
    from rich.text import Text

    from lab import agent as lab_agent
    from lab import registry
    from lab.models import LLM
    from lab.tools import Tools, schema_context

    console = _render.make_console()
    if not question.strip():
        console.print("[yellow]nothing to ask[/yellow]")
        return 2
    if not _litellm_installed():
        console.print("[red]the lab needs the 'lab' extra[/red]:  uv sync --extra lab")
        return 1

    cfg = lab_config.load()
    personas = registry.load_personas()
    persona = personas.get(persona_name)
    if persona is None:
        import difflib

        near = difflib.get_close_matches(persona_name, list(personas), n=1)
        msg = Text(f"unknown persona '{persona_name}'", style="red")
        if near:
            msg.append(f"  — did you mean {near[0]}?", style="dim")
        console.print(msg)
        console.print(Text(f"agents: {', '.join(personas) or '(none)'}", style="dim"))
        return 2

    import explore_eodhd  # type: ignore[import-not-found]
    import schema as sch  # type: ignore[import-not-found]

    llm = LLM(cfg)
    con = explore_eodhd.connect()
    tools = Tools(con)
    provenance = {
        "persona": persona.name,
        "model": cfg.resolve_model(persona.model),
        "data_root": str(explore_eodhd.EODHD_RAW_ROOT),
        "schema_version": sch.SCHEMA_VERSION,
    }
    try:
        bundle = lab_agent.run(
            question,
            persona=persona,
            llm=llm,
            tools=tools,
            schema_text=schema_context(),
            provenance=provenance,
        )
    except Exception as exc:  # model/connection errors -> friendly, not a traceback
        console.print(f"[red]{type(exc).__name__}[/red]: {exc}")
        return 1
    _render_answer(console, bundle, persona.name)
    return 0


def cmd_ask(argv: list[str]) -> int:
    """`ask <question>` -> run the default persona."""
    cfg = lab_config.load()
    return _run_agent(cfg.default_persona, " ".join(argv))


def cmd_agent(argv: list[str]) -> int:
    """`agent <persona> <task>` -> run a named persona."""
    if not argv:
        _render.make_console().print("usage: agent <persona> <task>")
        return 2
    return _run_agent(argv[0], " ".join(argv[1:]))


def cmd_run(argv: list[str]) -> int:
    """`lab run <skill> [args]` -> run an EDA playbook via the default persona."""
    from lab import registry

    console = _render.make_console()
    if not argv:
        console.print("usage: lab run <skill> [args]")
        return 2
    skills = registry.load_skills()
    skill = skills.get(argv[0])
    if skill is None:
        console.print(
            f"[red]unknown skill '{argv[0]}'[/red]. skills: {', '.join(skills)}"
        )
        return 2
    extra = " ".join(argv[1:])
    task = (
        f"Run the '{skill.name}' skill.\n\n{skill.body}\n\n"
        f"Inputs provided: {extra or '(none)'}"
    )
    return _run_agent(lab_config.load().default_persona, task)


def top_help() -> str:
    return (
        "lab -- Raw Data Lab (grounded EDA copilot)\n\n"
        "Commands:\n"
        "  config          Models, budget, cache and provider status\n"
        "  agents          List configured personas\n"
        "  skills          List EDA playbooks\n"
        "  run <skill>     Run an EDA playbook\n\n"
        "Top-level shell commands:\n"
        "  ask <question>          Ask the default persona\n"
        "  agent <name> <task>     Ask a named persona\n"
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return cmd_config([])
    command, rest = args[0], args[1:]
    dispatch = {
        "config": cmd_config,
        "agents": cmd_agents,
        "skills": cmd_skills,
        "run": cmd_run,
        "ask": cmd_ask,
        "agent": cmd_agent,
    }
    if command in ("-h", "--help", "help"):
        print(top_help())
        return 0
    if command in dispatch:
        return dispatch[command](rest)
    print(f"unknown lab command: {command!r}\n", file=sys.stderr)
    print(top_help())
    return 2


if __name__ == "__main__":
    sys.exit(main())
