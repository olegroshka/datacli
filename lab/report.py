"""Reproducible Markdown reports from a grounded answer (+ optional verdict).

The report embeds the exact queries and their results, plus provenance (persona,
model, data root, schema version), so the numbers regenerate deterministically on
re-run (temperature 0 + response cache) and every figure remains auditable.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from lab.agent import AnswerBundle
from lab.types import Finding
from lab.verify import Verdict


def slugify(text: str, *, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len] or "report"


def _md_table(finding: Finding, *, max_rows: int = 50) -> str:
    if not finding.columns:
        return "_(no columns)_"
    head = "| " + " | ".join(str(c) for c in finding.columns) + " |"
    sep = "| " + " | ".join("---" for _ in finding.columns) + " |"
    rows = [
        "| "
        + " | ".join("NULL" if v is None else str(v).replace("|", "\\|") for v in row)
        + " |"
        for row in finding.rows[:max_rows]
    ]
    out = "\n".join([head, sep, *rows])
    if len(finding.rows) > max_rows:
        out += f"\n\n_({len(finding.rows)} rows, showing {max_rows})_"
    return out


def _queries(findings: list[Finding], *, heading: str) -> list[str]:
    lines: list[str] = []
    for i, finding in enumerate(findings, 1):
        lines += [f"### {heading} {i}", "", "```sql", finding.sql, "```", ""]
        lines += [_md_table(finding), ""]
    return lines


def build(
    question: str,
    answer: AnswerBundle,
    *,
    title: str,
    generated_at: str,
    verdict: Verdict | None = None,
) -> str:
    """Render a full Markdown report string."""
    prov: dict[str, Any] = answer.findings[0].provenance if answer.findings else {}
    lines = [
        f"# Lab report — {title}",
        "",
        "| field | value |",
        "| --- | --- |",
        f"| persona | {prov.get('persona', '?')} |",
        f"| model | {prov.get('model', '?')} |",
        f"| data_root | {prov.get('data_root', '?')} |",
        f"| schema_version | {prov.get('schema_version', '?')} |",
        f"| generated | {generated_at} |",
        f"| steps | {answer.steps} |",
        f"| cost_usd | {answer.spent_usd:.4f} |",
    ]
    if verdict is not None:
        lines.append(f"| verdict | {verdict.label} |")
    lines += ["", "## Task", "", question.strip(), ""]
    lines += ["## Answer", "", answer.narrative or "_(no answer)_", ""]
    lines += ["## Evidence", ""]
    lines += _queries(answer.findings, heading="Query")

    if verdict is not None:
        lines += ["## Verification (skeptic)", "", f"**{verdict.label}**", ""]
        lines += [verdict.bundle.narrative or "", ""]
        lines += _queries(verdict.bundle.findings, heading="Verification query")

    return "\n".join(lines).rstrip() + "\n"


def save(markdown: str, reports_dir: Path, *, slug: str, stamp: str) -> Path:
    """Write the report to ``<reports_dir>/<slug>_<stamp>.md`` and return the path."""
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"{slug}_{stamp}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
