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
from lab.types import Finding  # noqa: E402


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_config_defaults() -> None:
    cfg = lab_config.load(Path("does-not-exist.toml"))
    assert cfg.default_persona == "analyst"
    assert cfg.budget.per_session_usd == 1.00
    assert cfg.models["local"].startswith("ollama/")
    # tier resolves to a model id; a raw id passes through untouched
    assert cfg.resolve_model("mid") == cfg.models["mid"]
    assert cfg.resolve_model("anthropic/custom") == "anthropic/custom"


def test_config_override(tmp_path: Path) -> None:
    toml = tmp_path / "datacli.toml"
    toml.write_text(
        "\n".join(
            [
                "[lab]",
                'default_persona = "auditor"',
                "[lab.budget]",
                "per_session_usd = 2.5",
                "[lab.models]",
                'mid = "anthropic/claude-sonnet-5-custom"',
            ]
        ),
        encoding="utf-8",
    )
    cfg = lab_config.load(toml)
    assert cfg.default_persona == "auditor"
    assert cfg.budget.per_session_usd == 2.5
    assert cfg.models["mid"] == "anthropic/claude-sonnet-5-custom"
    # untouched tiers keep their defaults
    assert cfg.models["local"] == lab_config.DEFAULT_MODELS["local"]


# --------------------------------------------------------------------------- #
# cache
# --------------------------------------------------------------------------- #
def test_cache_key_stability() -> None:
    msgs = [{"role": "user", "content": "hi"}]
    k1 = cache_mod.make_key("m", msgs, 0.0)
    k2 = cache_mod.make_key("m", msgs, 0.0)
    k3 = cache_mod.make_key("m", msgs, 0.7)  # temperature changes the key
    assert k1 == k2 and k1 != k3


def test_cache_roundtrip(tmp_path: Path) -> None:
    cache = cache_mod.ResponseCache(tmp_path)
    assert cache.get("missing") is None
    cache.put("k", {"text": "hello"})
    assert cache.get("k") == {"text": "hello"}
    assert cache.count() == 1


# --------------------------------------------------------------------------- #
# models (mocked -- never touches litellm or a live API)
# --------------------------------------------------------------------------- #
def _fake_response(text: str = "hello") -> object:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
    )


def _make_llm(tmp_path: Path, *, limit: float | None = 1.0):
    calls: list[dict] = []

    def fake_completion(*, model: str, messages: list, temperature: float) -> object:
        calls.append({"model": model, "messages": messages})
        return _fake_response()

    cfg = lab_config.LabConfig(
        cache_dir=tmp_path,
        budget=lab_config.Budget(per_session_usd=limit, warn_usd=None),
    )
    llm = lab_models.LLM(
        cfg,
        completion_fn=fake_completion,
        cost_fn=lambda resp: 0.002,
        cache=cache_mod.ResponseCache(tmp_path),
    )
    return llm, calls


def test_complete_returns_text_usage_cost(tmp_path: Path) -> None:
    llm, calls = _make_llm(tmp_path)
    c = llm.complete([{"role": "user", "content": "q"}], model="mid")
    assert c.text == "hello"
    assert c.cached is False
    assert c.usage.total_tokens == 15
    assert c.cost_usd == pytest.approx(0.002)
    assert llm.budget.spent_usd == pytest.approx(0.002)
    assert len(calls) == 1


def test_cache_hit_skips_model(tmp_path: Path) -> None:
    llm, calls = _make_llm(tmp_path)
    msgs = [{"role": "user", "content": "same"}]
    first = llm.complete(msgs, model="mid")
    second = llm.complete(msgs, model="mid")
    assert first.cached is False and second.cached is True
    assert second.text == "hello" and second.cost_usd == pytest.approx(0.002)
    assert len(calls) == 1  # model called once; second served from cache


def test_budget_ceiling_raises(tmp_path: Path) -> None:
    llm, _ = _make_llm(tmp_path, limit=0.001)  # below one call's cost
    llm.complete([{"role": "user", "content": "a"}], model="mid")  # spends 0.002
    with pytest.raises(lab_models.BudgetExceeded):
        llm.complete([{"role": "user", "content": "b"}], model="mid")


# --------------------------------------------------------------------------- #
# types
# --------------------------------------------------------------------------- #
def test_finding_row_count() -> None:
    f = Finding(claim="c", sql="SELECT 1", columns=["x"], rows=[(1,), (2,)])
    assert f.row_count == 2


# --------------------------------------------------------------------------- #
# cli smoke
# --------------------------------------------------------------------------- #
def test_lab_config_cli_runs(capsys: pytest.CaptureFixture[str]) -> None:
    from lab import cli as lab_cli

    assert lab_cli.main(["config"]) == 0
    assert "lab config" in capsys.readouterr().out
