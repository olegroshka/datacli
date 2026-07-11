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
from lab import pipeline as lab_pipeline  # noqa: E402
from lab import registry, report, tools  # noqa: E402


def _scripted_llm(tmp_path: Path, responses: list):
    """Scripted completions. An ``Exception`` item is raised on that call to
    simulate a model outage (rate limit / auth / a local server that's down)."""
    it = iter(responses)

    def fn(*, model: str, messages: list, temperature: float) -> object:
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=item))],
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
    con.execute("CREATE VIEW t AS SELECT * FROM (VALUES (1), (2)) v(id)")
    return con


_GEN = registry.Persona(name="hypothesizer", model="strong", role="generate")
_SKEPTIC = registry.Persona(name="skeptic", model="strong", role="verify")
_REPORTER = registry.Persona(name="reporter", model="mid", role="synthesise")


def test_pipeline_generator_skeptic_reporter(tmp_path: Path) -> None:
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT count(*) AS n FROM t\n```",  # generator queries
            "FINAL: there are 2 rows. HYPOTHESIS: the set grows over time.",  # generator
            "```sql\nSELECT count(*) AS n FROM t\n```",  # skeptic re-derives
            "FINAL: reproduced 2. VERDICT: CONFIRMED",  # skeptic verdict
            "FINAL: The data shows 2 rows. Caveat: tiny. HYPOTHESIS: it grows.",  # reporter
        ],
    )
    result = lab_pipeline.investigate(
        "row trends",
        generator=_GEN,
        skeptic=_SKEPTIC,
        reporter=_REPORTER,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert result.generator_name == "hypothesizer"
    assert len(result.generator.findings) == 1
    assert result.verdict.label == "CONFIRMED"
    assert "2 rows" in result.synthesis.narrative


def test_pipeline_degrades_without_skeptic_or_reporter(tmp_path: Path) -> None:
    llm = _scripted_llm(tmp_path, ["FINAL: nothing to compute"])
    result = lab_pipeline.investigate(
        "x",
        generator=_GEN,
        skeptic=None,
        reporter=None,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert result.verdict.label == "UNKNOWN"
    # with no reporter, the synthesis falls back to the generator's answer
    assert result.synthesis.narrative == result.generator.narrative


def test_pipeline_survives_skeptic_outage(tmp_path: Path) -> None:
    # generator succeeds; skeptic's model errors -> verdict UNKNOWN + note, but the
    # reporter still synthesises. No crash.
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: there are 2 rows.",
            RuntimeError("RateLimitError: quota exceeded"),  # skeptic call fails
            "FINAL: The data shows 2 rows.",  # reporter still runs
        ],
    )
    result = lab_pipeline.investigate(
        "x",
        generator=_GEN,
        skeptic=_SKEPTIC,
        reporter=_REPORTER,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert result.verdict.label == "UNKNOWN"
    assert "skeptic" in result.verdict.bundle.narrative
    assert "RateLimitError" in result.verdict.bundle.narrative
    assert "2 rows" in result.synthesis.narrative  # reporter degraded gracefully


def test_pipeline_survives_reporter_outage(tmp_path: Path) -> None:
    # generator + skeptic succeed; reporter's model errors -> synthesis falls back
    # to the generator's grounded answer with a note.
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: there are 2 rows.",
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: VERDICT: CONFIRMED",
            RuntimeError("AuthenticationError: no key"),  # reporter call fails
        ],
    )
    result = lab_pipeline.investigate(
        "x",
        generator=_GEN,
        skeptic=_SKEPTIC,
        reporter=_REPORTER,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert result.verdict.label == "CONFIRMED"
    assert "there are 2 rows" in result.synthesis.narrative  # generator fallback
    assert "reporter" in result.synthesis.narrative  # failure noted


def test_pipeline_survives_generator_outage(tmp_path: Path) -> None:
    # the foundation itself fails -> a clear note, skeptic/reporter skipped, no crash.
    llm = _scripted_llm(tmp_path, [RuntimeError("ConnectionError: ollama down")])
    result = lab_pipeline.investigate(
        "x",
        generator=_GEN,
        skeptic=_SKEPTIC,
        reporter=_REPORTER,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert result.verdict.label == "UNKNOWN"
    assert "generator" in result.generator.narrative
    assert "ConnectionError" in result.generator.narrative
    assert result.synthesis.narrative == result.generator.narrative


def test_pipeline_report(tmp_path: Path) -> None:
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: 2 rows.",
            "```sql\nSELECT count(*) AS n FROM t\n```",
            "FINAL: VERDICT: CONFIRMED",
            "FINAL: synthesis text",
        ],
    )
    result = lab_pipeline.investigate(
        "row trends",
        generator=_GEN,
        skeptic=_SKEPTIC,
        reporter=_REPORTER,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    md = report.build_pipeline(
        result, title="row trends", generated_at="2026-07-11T12:00:00"
    )
    assert "# Lab investigation — row trends" in md
    assert "| verdict | CONFIRMED |" in md
    assert "## Synthesis (reporter)" in md
    assert "## Findings (analyst)" in md
    assert "## Verification (skeptic)" in md
