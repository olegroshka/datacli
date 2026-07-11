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
_PY_BLOCK = re.compile(r"```python\s*(.+?)```", re.DOTALL | re.IGNORECASE)


@dataclass
class AnswerBundle:
    narrative: str
    findings: list[Finding] = field(default_factory=list)
    steps: int = 0
    spent_usd: float = 0.0
    budget_hit: bool = False
    figures: list[str] = field(default_factory=list)


def _system_prompt(
    persona: Persona, schema_text: str, max_rows: int, *, python: bool
) -> str:
    py = (
        "\n  2) a single ```python fenced block operating on `df` -- the LAST query's "
        "result as a pandas DataFrame (pandas as pd, numpy as np, matplotlib.pyplot "
        "as plt available). print() what you want returned; draw at most one figure. "
        "The executor is restricted (no file/network); use it for stats/plots SQL "
        "can't express, not to fetch data."
        if python
        else ""
    )
    final_num = "3" if python else "2"
    protocol = f"""
Tools: read-only SQL over DuckDB views (each view has a `lane` column).{
    " You may also run restricted Python on the last result." if python else ""}

Answer with EXACTLY ONE of:
  1) a single ```sql fenced block with one read-only SELECT/WITH query -- I will run
     it and return the columns and rows;{py}
  {final_num}) a line starting with `FINAL:` followed by your grounded answer.

Rules:
- Queries must be read-only (SELECT/WITH only); results are capped at {max_rows} rows.
- Iterate: query, read the result, query/analyse again if needed, then FINAL.
- NEVER put a number in FINAL that you did not see in a query or python result.

{schema_text}
""".strip()
    return f"{persona.role.strip()}\n\n{protocol}"


def _parse_action(text: str) -> tuple[str, str]:
    py = _PY_BLOCK.search(text)
    if py:
        return "python", py.group(1).strip()
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
    allow_python: bool = False,
    figure_dir: Any = None,
) -> AnswerBundle:
    base = dict(provenance or {})
    python_enabled = allow_python and "run_python" in persona.tools
    system = _system_prompt(persona, schema_text, tools.max_rows, python=python_enabled)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": question},
    ]
    findings: list[Finding] = []
    figures: list[str] = []
    last_df: Any = None

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
                figures=figures,
            )

        kind, payload = _parse_action(completion.text)
        if kind == "final":
            narrative = _maybe_review(question, payload, findings, persona, llm)
            return AnswerBundle(
                narrative=narrative,
                findings=findings,
                steps=step,
                spent_usd=llm.budget.spent_usd,
                figures=figures,
            )

        messages.append({"role": "assistant", "content": completion.text})

        if kind == "python":
            feedback = _run_python_step(
                payload,
                last_df,
                enabled=python_enabled,
                figure_dir=figure_dir,
                step=step,
                figures=figures,
            )
            messages.append({"role": "user", "content": feedback})
            continue

        result = tools.run_sql(payload)
        if result.ok:
            import pandas as pd

            last_df = pd.DataFrame(result.rows, columns=result.columns)
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
        figures=figures,
    )


def _evidence(findings: list[Finding], *, max_rows: int = 12) -> str:
    parts = []
    for i, f in enumerate(findings, 1):
        header = " | ".join(f.columns)
        body = "\n".join(
            " | ".join("NULL" if v is None else str(v) for v in row)
            for row in f.rows[:max_rows]
        )
        parts.append(f"Query {i}:\n{f.sql}\nResult:\n{header}\n{body}")
    return "\n\n".join(parts) if parts else "(no queries were run)"


def _maybe_review(
    question: str,
    draft: str,
    findings: list[Finding],
    persona: Persona,
    llm: LLM,
) -> str:
    """Re-synthesise the FINAL answer with a stronger ``review_model`` if set.

    The cheap/local loop does the grounded legwork; a more capable model writes the
    final answer -- but only from the evidence, so grounding is preserved.
    """
    review = persona.review_model
    if not review:
        return draft
    cfg = getattr(llm, "config", None)
    if cfg is not None and cfg.resolve_model(review) == cfg.resolve_model(
        persona.model
    ):
        return draft  # same underlying model -> nothing to gain

    messages = [
        {
            "role": "system",
            "content": (
                persona.role.strip()
                + "\n\nYou are writing the FINAL answer. Use ONLY numbers that "
                "appear in the evidence below; introduce no new figures. Be concise."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question:\n{question}\n\nGrounded evidence:\n"
                f"{_evidence(findings)}\n\nDraft answer from a faster model:\n{draft}\n\n"
                "Write the final grounded answer."
            ),
        },
    ]
    try:
        completion = llm.complete(messages, model=review, temperature=0.0)
    except BudgetExceeded:
        return draft  # keep the draft rather than fail
    return completion.text.strip() or draft


def _run_python_step(
    code: str,
    last_df: Any,
    *,
    enabled: bool,
    figure_dir: Any,
    step: int,
    figures: list[str],
) -> str:
    if not enabled:
        return (
            "Python execution is disabled. Use read-only SQL, or FINAL with what "
            "you have."
        )
    if last_df is None:
        return "Run a SQL query first; its result is provided to python as `df`."

    from lab import pyexec

    name = f"figure_{step}.png"
    res = pyexec.run_code(code, last_df, figure_dir=figure_dir, figure_name=name)
    if res.figure_path:
        figures.append(res.figure_path)
    if not res.ok:
        return f"Python error: {res.error}\nFix it or FINAL with what you have."
    note = res.stdout.strip() or "(no stdout)"
    if res.figure_path:
        note += f"\n[figure saved: {name}]"
    return "Python result:\n" + note
