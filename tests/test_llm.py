from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from llm import cache as cache_mod  # noqa: E402
from llm import tiers  # noqa: E402
from llm.models import LLM, BudgetExceeded  # noqa: E402


def _resp(text: str, prompt: int = 10, completion: int = 5) -> dict:
    return {
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
    }


def test_tiers_resolve_and_local_detection() -> None:
    assert tiers.resolve("local") == tiers.DEFAULT_MODELS["local"]
    assert tiers.resolve("local", {"local": "ollama/x"}) == "ollama/x"
    assert tiers.resolve("anthropic/claude-x") == "anthropic/claude-x"
    assert tiers.is_local("ollama/qwen2.5-coder:7b")
    assert not tiers.is_local("openai/gpt-4o-mini")


def test_complete_caches_and_charges(tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake(**kw):
        calls.append(kw)
        return _resp("hello")

    llm = LLM(
        models={"local": "ollama/x"},
        cache_dir=tmp_path,
        budget_usd=1.0,
        completion_fn=fake,
        cost_fn=lambda r: 0.25,
    )
    a = llm.complete([{"role": "user", "content": "hi"}], model="local")
    b = llm.complete([{"role": "user", "content": "hi"}], model="local")
    assert a.text == "hello" and a.model == "ollama/x" and not a.cached
    assert b.cached and b.cost_usd == 0.25 and b.usage.prompt_tokens == 10
    assert len(calls) == 1 and "response_format" not in calls[0]
    assert llm.budget.spent_usd == 0.25


def test_response_format_is_forwarded_and_part_of_cache_key(tmp_path: Path) -> None:
    seen: list[dict] = []

    def fake(**kw):
        seen.append(kw)
        return _resp('{"a": 1}')

    llm = LLM(cache_dir=tmp_path, completion_fn=fake, cost_fn=lambda r: 0.0)
    msgs = [{"role": "user", "content": "json please"}]
    llm.complete(msgs, model="ollama/x")
    llm.complete(msgs, model="ollama/x", response_format={"type": "json_object"})
    assert len(seen) == 2  # different cache keys
    assert seen[1]["response_format"] == {"type": "json_object"}
    # same call again -> cached, no third invocation
    llm.complete(msgs, model="ollama/x", response_format={"type": "json_object"})
    assert len(seen) == 2


def test_budget_ceiling_raises_before_call(tmp_path: Path) -> None:
    llm = LLM(
        cache_dir=tmp_path,
        budget_usd=0.10,
        completion_fn=lambda **kw: _resp("x"),
        cost_fn=lambda r: 0.10,
    )
    llm.complete([{"role": "user", "content": "1"}], model="m")
    with pytest.raises(BudgetExceeded):
        llm.complete([{"role": "user", "content": "2"}], model="m")


def test_embed_batches_cache_and_dims(tmp_path: Path) -> None:
    calls: list[dict] = []

    def fake_embed(**kw):
        calls.append(kw)
        return {
            "data": [{"embedding": [0.1, 0.2, 0.3]} for _ in kw["input"]],
            "usage": {"prompt_tokens": 7},
        }

    llm = LLM(
        models={"embed-local": "ollama/nomic-embed-text"},
        cache_dir=tmp_path,
        embedding_fn=fake_embed,
        cost_fn=lambda r: 0.0,
    )
    e = llm.embed(["a", "b"], model="embed-local")
    assert e.model == "ollama/nomic-embed-text" and e.dims == 3
    assert len(e.vectors) == 2 and not e.cached
    e2 = llm.embed(["a", "b"], model="embed-local")
    assert e2.cached and e2.vectors == e.vectors
    e3 = llm.embed(["a"], model="embed-local")  # different input -> new call
    assert not e3.cached and len(calls) == 2


def test_make_key_extra_changes_key() -> None:
    m = [{"role": "user", "content": "x"}]
    assert cache_mod.make_key("m", m, 0.0) != cache_mod.make_key(
        "m", m, 0.0, {"response_format": {"type": "json_object"}}
    )


def test_lab_shims_still_import() -> None:
    from lab import cache as lab_cache
    from lab import models as lab_models
    from lab import types as lab_types

    assert lab_cache.ResponseCache is cache_mod.ResponseCache
    assert issubclass(lab_models.LLM, LLM)
    assert lab_types.Usage.__module__ == "llm.types"
