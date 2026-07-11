"""The multi-agent investigation pipeline: generator -> skeptic -> reporter.

Composes the grounded loop (generator), the adversarial verifier (skeptic), and a
synthesiser (reporter) into one investigation. All three share the same LLM (one
session budget) and the same read-only tools, so the whole chain stays grounded --
the reporter may only synthesise numbers the generator computed and the skeptic
checked. Missing skeptic/reporter personas degrade gracefully.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lab.agent import AnswerBundle
from lab.agent import run as run_agent
from lab.registry import Persona
from lab.verify import Verdict
from lab.verify import verify as run_verify


@dataclass
class PipelineResult:
    topic: str
    generator: AnswerBundle
    verdict: Verdict
    synthesis: AnswerBundle
    generator_name: str = "generator"


def _evidence(bundle: AnswerBundle, *, max_rows: int = 8) -> str:
    parts = []
    for i, f in enumerate(bundle.findings, 1):
        header = " | ".join(f.columns)
        body = "\n".join(
            " | ".join("NULL" if v is None else str(v) for v in row)
            for row in f.rows[:max_rows]
        )
        parts.append(f"Query {i}:\n{f.sql}\nResult:\n{header}\n{body}")
    return "\n\n".join(parts) if parts else "(no queries)"


def _synthesis_task(topic: str, generator: AnswerBundle, verdict: Verdict) -> str:
    return (
        f"Investigation topic:\n{topic}\n\n"
        f"Analyst's grounded findings:\n{generator.narrative}\n\n"
        f"Analyst's evidence:\n{_evidence(generator)}\n\n"
        f"Skeptic's verdict: {verdict.label}\n{verdict.bundle.narrative}\n\n"
        "Write the synthesis (what the data shows, caveats, hypotheses to test). "
        "Use only numbers from the findings, and respect the verdict."
    )


def _degraded(role: str, name: str, err: Exception) -> str:
    """A one-line, actionable note when a stage's model is unavailable."""
    return (
        f"[{role} '{name}' unavailable: {type(err).__name__}: {err} "
        f"-- check the model/tier for this persona (billing, quota, or a local "
        f"server that isn't running)]"
    )


def investigate(
    topic: str,
    *,
    generator: Persona,
    skeptic: Persona | None,
    reporter: Persona | None,
    llm: Any,
    tools: Any,
    schema_text: str,
    provenance: dict[str, Any] | None = None,
    allow_python: bool = False,
    figure_dir: Any = None,
) -> PipelineResult:
    """Run generator -> skeptic -> reporter, degrading (not crashing) per stage.

    A model outage in one role (rate limit, auth, an Ollama server that's down)
    degrades that stage to a clear note and the investigation still returns a
    result -- the generator's grounded answer is the foundation, the skeptic and
    reporter are enhancements over it.
    """
    base = dict(provenance or {})

    gen_ok = True
    try:
        gen = run_agent(
            topic,
            persona=generator,
            llm=llm,
            tools=tools,
            schema_text=schema_text,
            provenance={**base, "persona": generator.name, "role": "generator"},
            allow_python=allow_python,
            figure_dir=figure_dir,
        )
    except Exception as err:  # foundation failed -- surface it, don't crash
        gen_ok = False
        gen = AnswerBundle(narrative=_degraded("generator", generator.name, err))

    verdict = Verdict(label="UNKNOWN")
    if skeptic is not None and gen_ok:
        try:
            verdict = run_verify(
                topic,
                gen,
                skeptic=skeptic,
                llm=llm,
                tools=tools,
                schema_text=schema_text,
                provenance={**base, "persona": skeptic.name, "role": "skeptic"},
            )
        except Exception as err:
            verdict = Verdict(
                label="UNKNOWN",
                bundle=AnswerBundle(narrative=_degraded("skeptic", skeptic.name, err)),
            )

    if reporter is not None and gen_ok:
        try:
            synthesis = run_agent(
                _synthesis_task(topic, gen, verdict),
                persona=reporter,
                llm=llm,
                tools=tools,
                schema_text=schema_text,
                provenance={**base, "persona": reporter.name, "role": "reporter"},
            )
        except Exception as err:
            # Fall back to the generator's grounded answer; note the reporter failure.
            synthesis = AnswerBundle(
                narrative=f"{gen.narrative}\n\n{_degraded('reporter', reporter.name, err)}"
            )
    else:
        synthesis = AnswerBundle(narrative=gen.narrative)

    return PipelineResult(
        topic=topic,
        generator=gen,
        verdict=verdict,
        synthesis=synthesis,
        generator_name=generator.name,
    )
