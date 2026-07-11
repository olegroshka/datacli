from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lab import agent as lab_agent  # noqa: E402
from lab import cache as cache_mod  # noqa: E402
from lab import config as lab_config  # noqa: E402
from lab import models as lab_models  # noqa: E402
from lab import registry, sqlguard, tools  # noqa: E402


# --------------------------------------------------------------------------- #
# SQL guard
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "query",
    [
        "SELECT 1",
        "select * from prices",
        "WITH x AS (SELECT 1 AS a) SELECT * FROM x",
        "  -- comment\n SELECT count(*) FROM dividends ",
    ],
)
def test_guard_accepts_readonly(query: str) -> None:
    ok, err, _ = sqlguard.validate(query)
    assert ok, err


@pytest.mark.parametrize(
    "query",
    [
        "DROP TABLE prices",
        "INSERT INTO t VALUES (1)",
        "SELECT 1; DROP TABLE t",  # stacked
        "WITH x AS (SELECT 1) DELETE FROM t",  # data-modifying CTE
        "COPY prices TO 'out.csv'",
        "PRAGMA database_list",
        "ATTACH 'x.db'",
        "",
    ],
)
def test_guard_rejects_writes(query: str) -> None:
    ok, _, _ = sqlguard.validate(query)
    assert not ok


def test_guard_injects_limit() -> None:
    ok, _, norm = sqlguard.validate("SELECT * FROM prices", max_rows=42)
    assert ok and "LIMIT 42" in norm
    ok, _, norm = sqlguard.validate("SELECT * FROM prices LIMIT 5")
    assert ok and norm.lower().count("limit") == 1  # not double-limited


# --------------------------------------------------------------------------- #
# registry (loads the shipped personas + skills)
# --------------------------------------------------------------------------- #
def test_registry_loads_personas_and_skills() -> None:
    personas = registry.load_personas()
    assert {"analyst", "auditor"} <= set(personas)
    assert personas["analyst"].tools == ["run_sql"]
    skills = registry.load_skills()
    assert "coverage-audit" in skills
    assert skills["coverage-audit"].inputs == ["lane"]
    assert skills["coverage-audit"].body  # frontmatter stripped, body present


# --------------------------------------------------------------------------- #
# tools over a real in-memory DuckDB
# --------------------------------------------------------------------------- #
def _con():
    duckdb = pytest.importorskip("duckdb")
    con = duckdb.connect()
    con.execute("CREATE VIEW t AS SELECT * FROM (VALUES (1,'a'),(2,'b')) v(id, name)")
    return con


def test_tools_run_sql_ok_and_guarded() -> None:
    t = tools.Tools(_con(), max_rows=10)
    res = t.run_sql("SELECT id, name FROM t ORDER BY id")
    assert res.ok and res.columns == ["id", "name"] and res.rows == [(1, "a"), (2, "b")]
    bad = t.run_sql("DROP VIEW t")
    assert not bad.ok and bad.error  # rejected before touching the DB


# --------------------------------------------------------------------------- #
# the grounded loop (scripted model, real DuckDB)
# --------------------------------------------------------------------------- #
def _scripted_llm(
    tmp_path: Path, responses: list[str], *, cost: float = 0.0, limit=1.0
):
    it = iter(responses)

    def fn(*, model: str, messages: list, temperature: float) -> object:
        text = next(it)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

    cfg = lab_config.LabConfig(
        cache_dir=tmp_path, budget=lab_config.Budget(per_session_usd=limit)
    )
    return lab_models.LLM(
        cfg,
        completion_fn=fn,
        cost_fn=lambda resp: cost,
        cache=cache_mod.ResponseCache(tmp_path),
    )


_PERSONA = registry.Persona(name="analyst", model="mid", temperature=0.0, role="test")


def test_loop_runs_query_then_answers(tmp_path: Path) -> None:
    llm = _scripted_llm(
        tmp_path,
        ["```sql\nSELECT id, name FROM t ORDER BY id\n```", "FINAL: there are 2 rows"],
    )
    bundle = lab_agent.run(
        "how many rows?",
        persona=_PERSONA,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="(schema)",
    )
    assert bundle.steps == 2
    assert len(bundle.findings) == 1
    assert bundle.findings[0].rows == [(1, "a"), (2, "b")]
    assert "2 rows" in bundle.narrative


def test_loop_final_immediately(tmp_path: Path) -> None:
    llm = _scripted_llm(tmp_path, ["FINAL: nothing to compute"])
    bundle = lab_agent.run(
        "hi", persona=_PERSONA, llm=llm, tools=tools.Tools(_con()), schema_text="s"
    )
    assert bundle.steps == 1 and bundle.findings == []


def test_loop_rejects_bad_sql_then_continues(tmp_path: Path) -> None:
    llm = _scripted_llm(
        tmp_path, ["```sql\nDROP VIEW t\n```", "FINAL: could not comply"]
    )
    bundle = lab_agent.run(
        "drop it", persona=_PERSONA, llm=llm, tools=tools.Tools(_con()), schema_text="s"
    )
    assert bundle.findings == []  # the write never produced a finding
    assert bundle.steps == 2


def test_loop_stops_on_budget(tmp_path: Path) -> None:
    llm = _scripted_llm(
        tmp_path,
        ["```sql\nSELECT 1\n```", "FINAL: unreachable"],
        cost=1.0,
        limit=0.5,  # the second model call exceeds the budget
    )
    bundle = lab_agent.run(
        "q", persona=_PERSONA, llm=llm, tools=tools.Tools(_con()), schema_text="s"
    )
    assert bundle.budget_hit is True
    assert len(bundle.findings) == 1  # first query still ran


# --------------------------------------------------------------------------- #
# action parser
# --------------------------------------------------------------------------- #
def test_parse_action() -> None:
    assert lab_agent._parse_action("```sql\nSELECT 1\n```") == ("sql", "SELECT 1")
    assert lab_agent._parse_action("FINAL: done") == ("final", "done")
    assert lab_agent._parse_action("just text") == ("final", "just text")


# --------------------------------------------------------------------------- #
# review_model: cheap loop, stronger final synthesis
# --------------------------------------------------------------------------- #
def test_review_model_synthesises_final(tmp_path: Path) -> None:
    persona = registry.Persona(name="a", model="local", review_model="mid", role="r")
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT id FROM t\n```",  # local loop does the legwork
            "FINAL: draft, 2 rows",  # local draft
            "reviewed: there are exactly 2 rows",  # stronger model's synthesis
        ],
    )
    bundle = lab_agent.run(
        "how many rows?",
        persona=persona,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
    )
    assert bundle.narrative == "reviewed: there are exactly 2 rows"
    assert len(bundle.findings) == 1  # the SQL still grounds the answer


def test_review_model_skipped_when_same_model(tmp_path: Path) -> None:
    persona = registry.Persona(name="a", model="local", review_model="local", role="r")
    llm = _scripted_llm(
        tmp_path, ["```sql\nSELECT id FROM t\n```", "FINAL: draft only"]
    )
    bundle = lab_agent.run(
        "q", persona=persona, llm=llm, tools=tools.Tools(_con()), schema_text="s"
    )
    assert bundle.narrative == "draft only"  # no second call when models are equal
