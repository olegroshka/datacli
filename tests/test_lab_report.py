from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lab import cache as cache_mod  # noqa: E402
from lab import config as lab_config  # noqa: E402
from lab import models as lab_models  # noqa: E402
from lab import registry  # noqa: E402
from lab import tools  # noqa: E402
from lab import report as lab_report  # noqa: E402
from lab import verify as lab_verify  # noqa: E402
from lab.agent import AnswerBundle  # noqa: E402
from lab.types import Finding  # noqa: E402


def _bundle() -> AnswerBundle:
    finding = Finding(
        claim="",
        sql="SELECT lane, count(*) AS n FROM dividends GROUP BY lane",
        columns=["lane", "n"],
        rows=[("us_common", 168714), ("uk_eu", 49079)],
        provenance={
            "persona": "analyst",
            "model": "ollama/qwen2.5-coder:7b",
            "data_root": "/data",
            "schema_version": 1,
        },
    )
    return AnswerBundle(
        narrative="us_common has the most dividends (168,714).",
        findings=[finding],
        steps=2,
        spent_usd=0.0,
    )


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #
def test_slugify() -> None:
    assert (
        lab_report.slugify("Coverage Audit: us_common!") == "coverage-audit-us-common"
    )
    assert lab_report.slugify("") == "report"


def test_report_build_is_grounded_and_deterministic() -> None:
    md = lab_report.build(
        "which lane has the most dividends?",
        _bundle(),
        title="dividends by lane",
        generated_at="2026-07-11T12:00:00",
    )
    assert "# Lab report — dividends by lane" in md
    assert "| persona | analyst |" in md
    assert "| schema_version | 1 |" in md
    assert "SELECT lane, count(*) AS n FROM dividends" in md  # the exact query
    assert "| us_common | 168714 |" in md  # the result, as a markdown table
    assert "us_common has the most dividends" in md
    # deterministic: same inputs -> identical bytes
    md2 = lab_report.build(
        "which lane has the most dividends?",
        _bundle(),
        title="dividends by lane",
        generated_at="2026-07-11T12:00:00",
    )
    assert md == md2


def test_report_save(tmp_path: Path) -> None:
    path = lab_report.save("# hi\n", tmp_path, slug="s", stamp="20260711-120000")
    assert path.exists() and path.name == "s_20260711-120000.md"
    assert path.read_text(encoding="utf-8") == "# hi\n"


def test_report_includes_verdict() -> None:
    verdict = lab_verify.Verdict(
        label="CONFIRMED",
        bundle=AnswerBundle(narrative="reproduced. VERDICT: CONFIRMED"),
    )
    md = lab_report.build(
        "q", _bundle(), title="t", generated_at="2026-07-11T12:00:00", verdict=verdict
    )
    assert "| verdict | CONFIRMED |" in md
    assert "## Verification (skeptic)" in md


# --------------------------------------------------------------------------- #
# verify
# --------------------------------------------------------------------------- #
def test_parse_verdict() -> None:
    assert lab_verify.parse_verdict("... VERDICT: CONFIRMED") == "CONFIRMED"
    assert lab_verify.parse_verdict("VERDICT: refuted, the number is 5") == "REFUTED"
    assert lab_verify.parse_verdict("no verdict here") == "UNKNOWN"


def _scripted_llm(tmp_path: Path, responses: list[str]):
    it = iter(responses)

    def fn(*, model: str, messages: list, temperature: float) -> object:
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=next(it)))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    cfg = lab_config.LabConfig(cache_dir=tmp_path)
    return lab_models.LLM(
        cfg,
        completion_fn=fn,
        cost_fn=lambda r: 0.0,
        cache=cache_mod.ResponseCache(tmp_path),
    )


def _con():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE VIEW t AS SELECT * FROM (VALUES (1),(2)) v(id)")
    return con


def test_verify_runs_skeptic_and_labels(tmp_path: Path) -> None:
    skeptic = registry.Persona(name="skeptic", model="strong", role="verify")
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: reproduced. VERDICT: CONFIRMED",
        ],
    )
    verdict = lab_verify.verify(
        "how many rows?",
        _bundle(),
        skeptic=skeptic,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert verdict.label == "CONFIRMED"
    assert len(verdict.bundle.findings) == 1  # the skeptic ran its own query
