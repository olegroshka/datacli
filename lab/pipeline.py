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
    base = dict(provenance or {})

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

    if skeptic is not None:
        verdict = run_verify(
            topic,
            gen,
            skeptic=skeptic,
            llm=llm,
            tools=tools,
            schema_text=schema_text,
            provenance={**base, "persona": skeptic.name, "role": "skeptic"},
        )
    else:
        verdict = Verdict(label="UNKNOWN")

    if reporter is not None:
        synthesis = run_agent(
            _synthesis_task(topic, gen, verdict),
            persona=reporter,
            llm=llm,
            tools=tools,
            schema_text=schema_text,
            provenance={**base, "persona": reporter.name, "role": "reporter"},
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
