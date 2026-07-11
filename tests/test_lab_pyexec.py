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
from lab import pyexec, registry, tools  # noqa: E402


# --------------------------------------------------------------------------- #
# the restricted executor
# --------------------------------------------------------------------------- #
def _df():
    pd = pytest.importorskip("pandas")
    return pd.DataFrame({"x": [1, 2, 3, 4]})


def test_pyexec_runs_and_captures_stdout() -> None:
    pytest.importorskip("pyarrow")
    res = pyexec.run_code("print(df.shape); print(round(df['x'].mean(), 2))", _df())
    assert res.ok and res.stdout.strip() == "(4, 1)\n2.5"


def test_pyexec_blocks_open() -> None:
    pytest.importorskip("pyarrow")
    res = pyexec.run_code("open('hack.txt', 'w')", _df())
    assert not res.ok and "open" in res.error


def test_pyexec_blocks_forbidden_import() -> None:
    pytest.importorskip("pyarrow")
    res = pyexec.run_code("import os", _df())
    assert not res.ok and "not allowed" in res.error


def test_pyexec_timeout() -> None:
    pytest.importorskip("pyarrow")
    res = pyexec.run_code("while True:\n    pass", _df(), timeout_s=3)
    assert not res.ok and "timed out" in res.error


def test_pyexec_saves_figure(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    pytest.importorskip("matplotlib")
    code = "import matplotlib.pyplot as plt\nplt.plot(df['x'])\n"
    res = pyexec.run_code(code, _df(), figure_dir=tmp_path, figure_name="fig.png")
    assert res.ok and res.figure_path is not None
    assert Path(res.figure_path).exists()


# --------------------------------------------------------------------------- #
# the python action in the agent loop
# --------------------------------------------------------------------------- #
def test_parse_action_detects_python() -> None:
    assert lab_agent._parse_action("```python\nprint(1)\n```") == ("python", "print(1)")


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
    con.execute("CREATE VIEW t AS SELECT * FROM (VALUES (1), (2)) v(id)")
    return con


_QUANT = registry.Persona(
    name="quant", model="mid", tools=["run_sql", "run_python"], role="quant"
)


def test_agent_runs_python_on_query_result(tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    llm = _scripted_llm(
        tmp_path,
        [
            "```sql\nSELECT id FROM t ORDER BY id\n```",  # pull a df
            "```python\nprint('mean', df['id'].mean())\n```",  # analyse it
            "FINAL: the mean id is 1.5",
        ],
    )
    bundle = lab_agent.run(
        "mean id?",
        persona=_QUANT,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
        allow_python=True,
    )
    assert bundle.steps == 3
    assert len(bundle.findings) == 1  # the SQL query
    assert "1.5" in bundle.narrative


def test_agent_python_disabled_when_flag_off(tmp_path: Path) -> None:
    llm = _scripted_llm(tmp_path, ["```python\nprint(1)\n```", "FINAL: could not"])
    bundle = lab_agent.run(
        "q",
        persona=_QUANT,
        llm=llm,
        tools=tools.Tools(_con()),
        schema_text="s",
        allow_python=False,  # gated off
    )
    assert bundle.figures == [] and bundle.steps == 2


def test_config_allow_python_default_off() -> None:
    assert lab_config.load(Path("nope.toml")).allow_python is False
