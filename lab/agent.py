"""The grounded analyst loop: plan -> SQL -> validate -> execute -> narrate.

Model-agnostic by design. The model answers with EITHER a single ```sql block (we
validate + run it and feed back the rows) OR a `FINAL:` answer. This text protocol
works with local/free models too, and -- crucially -- every number in the final
answer is backed by an executed query captured as a Finding.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from lab.models import LLM, BudgetExceeded
from lab.registry import Persona
from lab.tools import Tools, result_to_text
from lab.types import Finding

_SQL_BLOCK = re.compile(r"```sql\s*(.+?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class AnswerBundle:
    narrative: str
    findings: list[Finding] = field(default_factory=list)
    steps: int = 0
    spent_usd: float = 0.0
    budget_hit: bool = False


def _system_prompt(persona: Persona, schema_text: str, max_rows: int) -> str:
    protocol = f"""
You have ONE tool: read-only SQL over DuckDB views (each view has a `lane` column).

Answer with EXACTLY ONE of:
  1) a single ```sql fenced block with one read-only SELECT/WITH query -- I will run
     it and return the columns and rows; or
  2) a line starting with `FINAL:` followed by your grounded answer.

Rules:
- Queries must be read-only (SELECT/WITH only); results are capped at {max_rows} rows.
- Iterate: query, read the result, query again if needed, then FINAL.
- NEVER put a number in FINAL that you did not see in a query result.

{schema_text}
""".strip()
    return f"{persona.role.strip()}\n\n{protocol}"


def _parse_action(text: str) -> tuple[str, str]:
    match = _SQL_BLOCK.search(text)
    if match:
        return "sql", match.group(1).strip()
    idx = text.upper().find("FINAL:")
    if idx != -1:
        return "final", text[idx + len("FINAL:") :].strip()
    return "final", text.strip()  # fallback: treat a bare reply as the answer


def run(
    question: str,
    *,
    persona: Persona,
    llm: LLM,
    tools: Tools,
    schema_text: str,
    provenance: dict[str, Any] | None = None,
    max_steps: int = 5,
) -> AnswerBundle:
    base = dict(provenance or {})
    system = _system_prompt(persona, schema_text, tools.max_rows)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    findings: list[Finding] = []

    for step in range(1, max_steps + 1):
        try:
            completion = llm.complete(
                messages, model=persona.model, temperature=persona.temperature
            )
        except BudgetExceeded as exc:
            return AnswerBundle(
                narrative=f"(stopped: {exc})",
                findings=findings,
                steps=step - 1,
                spent_usd=llm.budget.spent_usd,
                budget_hit=True,
            )

        kind, payload = _parse_action(completion.text)
        if kind == "final":
            return AnswerBundle(
                narrative=payload,
                findings=findings,
                steps=step,
                spent_usd=llm.budget.spent_usd,
            )

        result = tools.run_sql(payload)
        messages.append({"role": "assistant", "content": completion.text})
        if result.ok:
            findings.append(
                Finding(
                    claim="",
                    sql=result.sql,
                    columns=result.columns,
                    rows=result.rows,
                    provenance={**base, "step": step, "cached": completion.cached},
                )
            )
            messages.append(
                {"role": "user", "content": "Result:\n" + result_to_text(result)}
            )
        else:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"Your query was rejected or failed: {result.error}\n"
                        "Return a corrected read-only query, or FINAL with what you have."
                    ),
                }
            )

    return AnswerBundle(
        narrative="(reached the step limit without a FINAL answer)",
        findings=findings,
        steps=max_steps,
        spent_usd=llm.budget.spent_usd,
    )
