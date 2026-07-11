"""Adversarial verification: re-derive an answer's numbers with a skeptic persona.

The skeptic runs the same grounded loop -- it must write its own read-only queries
-- so a verdict is itself grounded, not a vibe check. Its ``VERDICT:`` line is
parsed into a label the report and console can act on.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lab.agent import AnswerBundle
from lab.agent import run as run_agent
from lab.registry import Persona
from lab.types import Finding

_VERDICT = re.compile(r"VERDICT:\s*(CONFIRMED|REFUTED|UNCERTAIN)", re.IGNORECASE)


@dataclass
class Verdict:
    label: str  # CONFIRMED | REFUTED | UNCERTAIN | UNKNOWN
    bundle: AnswerBundle = field(default_factory=lambda: AnswerBundle(narrative=""))


def _evidence(findings: list[Finding], *, max_rows: int = 8) -> str:
    parts = []
    for i, f in enumerate(findings, 1):
        header = " | ".join(f.columns)
        body = "\n".join(
            " | ".join("NULL" if v is None else str(v) for v in row)
            for row in f.rows[:max_rows]
        )
        parts.append(f"Query {i}:\n{f.sql}\nResult:\n{header}\n{body}")
    return "\n\n".join(parts) if parts else "(the analyst ran no queries)"


def parse_verdict(text: str) -> str:
    match = _VERDICT.search(text)
    return match.group(1).upper() if match else "UNKNOWN"


def verify(
    question: str,
    answer: AnswerBundle,
    *,
    skeptic: Persona,
    llm: Any,
    tools: Any,
    schema_text: str,
    provenance: dict[str, Any] | None = None,
) -> Verdict:
    task = (
        f"The analyst was asked:\n{question}\n\n"
        f"The analyst answered:\n{answer.narrative}\n\n"
        f"The analyst's queries and results:\n{_evidence(answer.findings)}\n\n"
        "Independently verify the key numbers with your own read-only queries, "
        "then end with a VERDICT line."
    )
    bundle = run_agent(
        task,
        persona=skeptic,
        llm=llm,
        tools=tools,
        schema_text=schema_text,
        provenance=provenance,
    )
    return Verdict(label=parse_verdict(bundle.narrative), bundle=bundle)
