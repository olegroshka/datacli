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
from typing import Any

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
    table.add_row("reports", Text(str(cfg.reports_dir), style="dim"))
    pyexec = (
        Text("enabled (restricted, trusted-local)", style="yellow")
        if cfg.allow_python
        else Text("disabled — set [lab].allow_python to enable", style="dim")
    )
    table.add_row("python exec", pyexec)
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
        model = p.model + (f" → {p.review_model}" if p.review_model else "")
        table.add_row(p.name, Text(model, style="dim"), p.description)
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
def _render_findings(console: Any, findings: list[Any], *, label: str) -> None:
    from rich.syntax import Syntax

    for i, finding in enumerate(findings, 1):
        console.rule(f"{label} {i}", style="dim", align="left")
        console.print(
            Syntax(finding.sql, "sql", background_color="default", word_wrap=True)
        )
        table = _render.minimal_table()
        for column in finding.columns:
            table.add_column(str(column), no_wrap=True)
        for row in finding.rows[:50]:
            table.add_row(*("NULL" if v is None else str(v) for v in row))
        console.print(table)


def _render_answer(console: Any, bundle: Any, persona_name: str) -> None:
    from rich.panel import Panel
    from rich.text import Text

    console.print(
        Panel(
            bundle.narrative or "(no answer)",
            title=f"answer · {persona_name}",
            border_style="cyan",
        )
    )
    _render_findings(console, bundle.findings, label="query")
    console.print(
        Text(
            f"{bundle.steps} step(s) · {len(bundle.findings)} query(ies) · "
            f"${bundle.spent_usd:.4f}",
            style="dim",
        )
    )


def _render_verdict(console: Any, verdict: Any) -> None:
    from rich.panel import Panel

    color = {"CONFIRMED": "green", "REFUTED": "bold red", "UNCERTAIN": "yellow"}.get(
        verdict.label, "dim"
    )
    console.print(
        Panel(
            verdict.bundle.narrative or "(no verdict)",
            title=f"skeptic · {verdict.label}",
            border_style=color,
        )
    )
    _render_findings(console, verdict.bundle.findings, label="verify")


def _split_flags(argv: list[str]) -> tuple[set[str], list[str]]:
    flags = {a for a in argv if a.startswith("--")}
    rest = [a for a in argv if not a.startswith("--")]
    return flags, rest


def _agent_context(cfg: Any) -> tuple[Any, Any, str, dict[str, Any]]:
    """Build the shared LLM, tools, schema and provenance base for a lab run."""
    import explore_eodhd  # type: ignore[import-not-found]
    import schema as sch  # type: ignore[import-not-found]

    from lab import data as lab_data
    from lab.models import LLM
    from lab.tools import Tools

    llm = LLM(cfg)  # shared across all personas in a run -> one session budget
    con = lab_data.connect()  # eodhd views + macro (if fetched)
    tools = Tools(con)
    schema_text = lab_data.schema_text(con)
    prov = {
        "data_root": str(explore_eodhd.EODHD_RAW_ROOT),
        "schema_version": sch.SCHEMA_VERSION,
    }
    return llm, tools, schema_text, prov


def _run_agent(
    persona_name: str,
    question: str,
    *,
    verify: bool = False,
    report: bool = False,
    title: str | None = None,
) -> int:
    from datetime import datetime

    from rich.text import Text

    from lab import agent as lab_agent
    from lab import registry
    from lab import report as lab_report
    from lab import verify as lab_verify

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
    if verify and "skeptic" not in personas:
        console.print("[yellow]no 'skeptic' persona; skipping verification[/yellow]")
        verify = False

    llm, tools, schema_text, prov = _agent_context(cfg)
    provenance = {
        "persona": persona.name,
        "model": cfg.resolve_model(persona.model),
        **prov,
    }
    try:
        bundle = lab_agent.run(
            question,
            persona=persona,
            llm=llm,
            tools=tools,
            schema_text=schema_text,
            provenance=provenance,
            allow_python=cfg.allow_python,
            figure_dir=cfg.reports_dir / "figures",
        )
    except Exception as exc:  # model/connection errors -> friendly, not a traceback
        console.print(f"[red]{type(exc).__name__}[/red]: {exc}")
        return 1
    _render_answer(console, bundle, persona.name)
    for fig in bundle.figures:
        console.print(f"[dim]figure -> {fig}[/dim]")

    verdict = None
    if verify:
        skeptic = personas["skeptic"]
        try:
            verdict = lab_verify.verify(
                question,
                bundle,
                skeptic=skeptic,
                llm=llm,
                tools=tools,
                schema_text=schema_text,
                provenance={
                    **provenance,
                    "persona": skeptic.name,
                    "model": cfg.resolve_model(skeptic.model),
                },
            )
        except Exception as exc:
            console.print(f"[red]verification failed[/red]: {exc}")
        else:
            _render_verdict(console, verdict)

    if report:
        report_title = title or (question.strip().splitlines()[0][:60] or "report")
        markdown = lab_report.build(
            question,
            bundle,
            title=report_title,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            verdict=verdict,
        )
        path = lab_report.save(
            markdown,
            cfg.reports_dir,
            slug=lab_report.slugify(report_title),
            stamp=datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        console.print(Text(f"report written -> {path}", style="green"))
    return 0


def _render_pipeline(console: Any, result: Any) -> None:
    from rich.panel import Panel

    console.rule("generator", style="dim", align="left")
    _render_answer(console, result.generator, result.generator_name)
    if result.verdict.label != "UNKNOWN":
        _render_verdict(console, result.verdict)
    console.print(
        Panel(
            result.synthesis.narrative or "(no synthesis)",
            title="synthesis · reporter",
            border_style="bold cyan",
        )
    )


def _run_pipeline(topic: str, generator_name: str, *, report: bool = True) -> int:
    from datetime import datetime

    from rich.text import Text

    from lab import pipeline as lab_pipeline
    from lab import registry
    from lab import report as lab_report

    console = _render.make_console()
    if not topic.strip():
        console.print("[yellow]nothing to investigate[/yellow]")
        return 2
    if not _litellm_installed():
        console.print("[red]the lab needs the 'lab' extra[/red]:  uv sync --extra lab")
        return 1

    cfg = lab_config.load()
    personas = registry.load_personas()
    generator = personas.get(generator_name)
    if generator is None:
        console.print(f"[red]unknown persona '{generator_name}'[/red]")
        return 2

    llm, tools, schema_text, prov = _agent_context(cfg)
    try:
        result = lab_pipeline.investigate(
            topic,
            generator=generator,
            skeptic=personas.get("skeptic"),
            reporter=personas.get("reporter"),
            llm=llm,
            tools=tools,
            schema_text=schema_text,
            provenance=prov,
            allow_python=cfg.allow_python,
            figure_dir=cfg.reports_dir / "figures",
        )
    except Exception as exc:
        console.print(f"[red]{type(exc).__name__}[/red]: {exc}")
        return 1
    _render_pipeline(console, result)
    console.print(Text(f"session spend: ${llm.budget.spent_usd:.4f}", style="dim"))

    if report:
        title = topic.strip().splitlines()[0][:60] or "investigation"
        markdown = lab_report.build_pipeline(
            result,
            title=title,
            generated_at=datetime.now().isoformat(timespec="seconds"),
        )
        path = lab_report.save(
            markdown,
            cfg.reports_dir,
            slug="investigate-" + lab_report.slugify(title),
            stamp=datetime.now().strftime("%Y%m%d-%H%M%S"),
        )
        console.print(Text(f"report written -> {path}", style="green"))
    return 0


def cmd_investigate(argv: list[str]) -> int:
    """`investigate <topic> [--generator=<persona>] [--no-report]` -> multi-agent run."""
    flags, rest = _split_flags(argv)
    generator = lab_config.load().default_persona
    for flag in flags:
        if flag.startswith("--generator="):
            generator = flag.split("=", 1)[1]
    return _run_pipeline(" ".join(rest), generator, report="--no-report" not in flags)


def cmd_ask(argv: list[str]) -> int:
    """`ask <question> [--verify] [--report]` -> run the default persona."""
    flags, rest = _split_flags(argv)
    cfg = lab_config.load()
    return _run_agent(
        cfg.default_persona,
        " ".join(rest),
        verify="--verify" in flags,
        report="--report" in flags,
    )


def cmd_agent(argv: list[str]) -> int:
    """`agent <persona> <task> [--verify] [--report]` -> run a named persona."""
    flags, rest = _split_flags(argv)
    if not rest:
        _render.make_console().print(
            "usage: agent <persona> <task> [--verify] [--report]"
        )
        return 2
    return _run_agent(
        rest[0],
        " ".join(rest[1:]),
        verify="--verify" in flags,
        report="--report" in flags,
    )


def cmd_run(argv: list[str]) -> int:
    """`lab run <skill> [args] [--verify] [--no-report]` -> playbook -> report."""
    from lab import registry

    console = _render.make_console()
    flags, rest = _split_flags(argv)
    if not rest:
        console.print("usage: lab run <skill> [args] [--verify] [--no-report]")
        return 2
    skills = registry.load_skills()
    skill = skills.get(rest[0])
    if skill is None:
        console.print(
            f"[red]unknown skill '{rest[0]}'[/red]. skills: {', '.join(skills)}"
        )
        return 2
    extra = " ".join(rest[1:])
    task = (
        f"Run the '{skill.name}' skill.\n\n{skill.body}\n\n"
        f"Inputs provided: {extra or '(none)'}"
    )
    return _run_agent(
        lab_config.load().default_persona,
        task,
        verify="--verify" in flags,
        report="--no-report" not in flags,  # reports are the point of `lab run`
        title=skill.name,
    )


def top_help() -> str:
    return (
        "lab -- Raw Data Lab (grounded EDA copilot)\n\n"
        "Commands:\n"
        "  config                Models, budget, cache and provider status\n"
        "  agents                List configured personas\n"
        "  skills                List EDA playbooks\n"
        "  run <skill> [--verify]  Run a playbook -> reproducible report\n\n"
        "Top-level shell commands:\n"
        "  ask <question> [--verify] [--report]       Ask the default persona\n"
        "  agent <name> <task> [--verify] [--report]  Ask a named persona\n"
        "  investigate <topic> [--generator=<name>]   Generator -> skeptic -> reporter\n"
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
        "investigate": cmd_investigate,
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
