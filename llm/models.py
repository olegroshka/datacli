"""Provider-agnostic model access with a budget and a response cache.

Wraps LiteLLM (one interface across Anthropic / OpenAI / Ollama) but takes the
completion / embedding / cost functions by injection, so the whole layer is
unit-testable without LiteLLM installed and without ever hitting a live API.
LiteLLM is imported lazily, only when a real call is made.

Typical use::

    llm = LLM(models={"local": "ollama/qwen2.5-coder:7b"}, cache_dir=Path(".cache"))
    out = llm.complete([{"role": "user", "content": "..."}], model="local")
    vec = llm.embed(["text a", "text b"], model="embed-local")
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from llm import cache as cache_mod
from llm import tiers
from llm.types import Completion, Embedding, Usage


class BudgetExceeded(RuntimeError):
    """Raised when a call would run past the per-session spend ceiling."""


class SpendTracker:
    """Accumulates cost and enforces a hard ceiling before each call."""

    def __init__(self, limit_usd: float | None, warn_usd: float | None = None) -> None:
        self.limit_usd = limit_usd
        self.warn_usd = warn_usd
        self.spent_usd = 0.0

    def check(self) -> None:
        if self.limit_usd is not None and self.spent_usd >= self.limit_usd:
            raise BudgetExceeded(
                f"session budget of ${self.limit_usd:.2f} reached "
                f"(spent ${self.spent_usd:.4f}); raise the budget to continue"
            )

    def charge(self, amount_usd: float) -> None:
        self.spent_usd += max(amount_usd, 0.0)

    @property
    def warned(self) -> bool:
        return self.warn_usd is not None and self.spent_usd >= self.warn_usd


class CompletionFn(Protocol):
    def __call__(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> Any: ...


class EmbeddingFn(Protocol):
    def __call__(self, *, model: str, input: list[str]) -> Any: ...


def _extract_text(resp: Any) -> str:
    try:
        return str(resp.choices[0].message.content)
    except (AttributeError, TypeError, KeyError, IndexError):
        return str(resp["choices"][0]["message"]["content"])


def _extract_usage(resp: Any) -> Usage:
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if usage is None:
        return Usage()
    get = (lambda k: getattr(usage, k, 0)) if not isinstance(usage, dict) else usage.get
    return Usage(
        prompt_tokens=int(get("prompt_tokens") or 0),
        completion_tokens=int(get("completion_tokens") or 0),
    )


def _extract_vectors(resp: Any) -> list[list[float]]:
    data = getattr(resp, "data", None)
    if data is None and isinstance(resp, dict):
        data = resp.get("data")
    vectors: list[list[float]] = []
    for item in data or []:
        emb = getattr(item, "embedding", None)
        if emb is None and isinstance(item, dict):
            emb = item.get("embedding")
        vectors.append([float(x) for x in (emb or [])])
    return vectors


class LLM:
    """Model access: tier resolution, cache, and budget in one place."""

    def __init__(
        self,
        *,
        models: Mapping[str, str] | None = None,
        cache_dir: Path | None = None,
        budget_usd: float | None = None,
        warn_usd: float | None = None,
        completion_fn: CompletionFn | None = None,
        embedding_fn: EmbeddingFn | None = None,
        cost_fn: Callable[[Any], float] | None = None,
        cache: cache_mod.ResponseCache | None = None,
    ) -> None:
        """
        Args:
            models: Tier overrides merged over :data:`llm.tiers.DEFAULT_MODELS`.
            cache_dir: Where cached responses live (ignored when ``cache`` given).
            budget_usd: Hard per-session spend ceiling; ``None`` = unbounded.
            warn_usd: Soft threshold exposed via ``budget.warned``.
            completion_fn / embedding_fn / cost_fn: Injected implementations
                (tests, custom providers); default to LiteLLM, bound lazily.
            cache: A ready cache instance (else one is built under ``cache_dir``).
        """
        self.models = dict(models or {})
        self._completion_fn = completion_fn
        self._embedding_fn = embedding_fn
        self._cost_fn = cost_fn
        self.cache = cache or cache_mod.ResponseCache(cache_dir or Path(".llm_cache"))
        self.budget = SpendTracker(budget_usd, warn_usd)

    def resolve_model(self, tier_or_id: str) -> str:
        return tiers.resolve(tier_or_id, self.models)

    # -- lazy LiteLLM bindings (only touched on a real, uncached call) -------- #
    @staticmethod
    def _litellm() -> Any:
        """Import LiteLLM and apply our global policy once.

        We send ``temperature=0`` for deterministic grounding, but some strong
        models (reasoning tiers) only accept ``temperature=1`` and LiteLLM raises
        ``UnsupportedParamsError`` otherwise. ``drop_params`` lets LiteLLM drop a
        parameter a given model can't honour instead of failing.
        """
        import litellm  # type: ignore[import-not-found]

        litellm.drop_params = True
        return litellm

    def _litellm_completion(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> Any:
        litellm = self._litellm()
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:
            kwargs["response_format"] = response_format
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return litellm.completion(**kwargs)

    def _litellm_embedding(self, *, model: str, input: list[str]) -> Any:
        litellm = self._litellm()
        return litellm.embedding(model=model, input=input)

    def _litellm_cost(self, resp: Any) -> float:
        try:
            litellm = self._litellm()

            return float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            return 0.0

    # -- public API ----------------------------------------------------------- #
    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.0,
        response_format: dict[str, Any] | None = None,
        max_tokens: int | None = None,
    ) -> Completion:
        """Run one completion. ``model`` is a tier name or a raw model id.

        Returns a cached result for free when available; otherwise checks the
        budget, calls the model, charges the cost, and caches the response.
        ``response_format`` (e.g. ``{"type": "json_object"}``) and ``max_tokens``
        are passed through and are part of the cache key.
        """
        model_id = self.resolve_model(model)
        extra: dict[str, Any] = {}
        if response_format:
            extra["response_format"] = response_format
        if max_tokens is not None:
            extra["max_tokens"] = max_tokens
        key = cache_mod.make_key(model_id, messages, temperature, extra or None)

        hit = self.cache.get(key)
        if hit is not None:
            usage = hit.get("usage", {})
            return Completion(
                text=hit.get("text", ""),
                model=model_id,
                usage=Usage(
                    prompt_tokens=int(usage.get("prompt_tokens", 0)),
                    completion_tokens=int(usage.get("completion_tokens", 0)),
                ),
                cost_usd=float(hit.get("cost_usd", 0.0)),
                cached=True,
            )

        self.budget.check()
        fn = self._completion_fn or self._litellm_completion
        call_kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": temperature,
        }
        if response_format is not None:  # keep plain injected fns compatible
            call_kwargs["response_format"] = response_format
        if max_tokens is not None:
            call_kwargs["max_tokens"] = max_tokens
        resp = fn(**call_kwargs)
        text = _extract_text(resp)
        usage = _extract_usage(resp)
        cost = (self._cost_fn or self._litellm_cost)(resp)
        self.budget.charge(cost)

        self.cache.put(
            key,
            {
                "text": text,
                "model": model_id,
                "usage": {
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                },
                "cost_usd": cost,
            },
        )
        return Completion(
            text=text, model=model_id, usage=usage, cost_usd=cost, cached=False
        )

    def embed(self, texts: list[str], *, model: str) -> Embedding:
        """Embed a batch of texts. ``model`` is a tier name or a raw model id.

        Cached per batch (same model + same inputs), budgeted like completions.
        """
        model_id = self.resolve_model(model)
        key = cache_mod.make_key(model_id, [], 0.0, {"embed": texts})
        hit = self.cache.get(key)
        if hit is not None:
            usage = hit.get("usage", {})
            return Embedding(
                vectors=[[float(x) for x in v] for v in hit.get("vectors", [])],
                model=model_id,
                usage=Usage(prompt_tokens=int(usage.get("prompt_tokens", 0))),
                cost_usd=float(hit.get("cost_usd", 0.0)),
                cached=True,
            )
        self.budget.check()
        fn = self._embedding_fn or self._litellm_embedding
        resp = fn(model=model_id, input=texts)
        vectors = _extract_vectors(resp)
        usage = _extract_usage(resp)
        cost = (self._cost_fn or self._litellm_cost)(resp)
        self.budget.charge(cost)
        self.cache.put(
            key,
            {
                "vectors": vectors,
                "model": model_id,
                "usage": {"prompt_tokens": usage.prompt_tokens},
                "cost_usd": cost,
            },
        )
        return Embedding(
            vectors=vectors, model=model_id, usage=usage, cost_usd=cost, cached=False
        )
