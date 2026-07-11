"""Provider-agnostic model access with a budget and a response cache.

Wraps LiteLLM (one interface across Anthropic / OpenAI / Ollama) but takes the
completion + cost functions by injection, so the whole layer is unit-testable
without LiteLLM installed and without ever hitting a live API. LiteLLM is imported
lazily, only when a real call is made.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol

from lab import cache as cache_mod
from lab.config import LabConfig
from lab.types import Completion, Usage


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
                f"(spent ${self.spent_usd:.4f}); raise [lab.budget] to continue"
            )

    def charge(self, amount_usd: float) -> None:
        self.spent_usd += max(amount_usd, 0.0)

    @property
    def warned(self) -> bool:
        return self.warn_usd is not None and self.spent_usd >= self.warn_usd


class CompletionFn(Protocol):
    def __call__(
        self, *, model: str, messages: list[dict[str, Any]], temperature: float
    ) -> Any: ...


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


class LLM:
    """Model access: tier resolution, cache, and budget in one place."""

    def __init__(
        self,
        config: LabConfig,
        *,
        completion_fn: CompletionFn | None = None,
        cost_fn: Callable[[Any], float] | None = None,
        cache: cache_mod.ResponseCache | None = None,
    ) -> None:
        self.config = config
        self._completion_fn = completion_fn
        self._cost_fn = cost_fn
        self.cache = cache or cache_mod.ResponseCache(config.cache_dir)
        self.budget = SpendTracker(
            config.budget.per_session_usd, config.budget.warn_usd
        )

    # -- lazy LiteLLM bindings (only touched on a real, uncached call) -------- #
    def _litellm_completion(
        self, *, model: str, messages: list[dict[str, Any]], temperature: float
    ) -> Any:
        import litellm  # type: ignore[import-not-found]

        return litellm.completion(
            model=model, messages=messages, temperature=temperature
        )

    def _litellm_cost(self, resp: Any) -> float:
        try:
            import litellm  # type: ignore[import-not-found]

            return float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            return 0.0

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.0,
    ) -> Completion:
        """Run one completion. ``model`` is a tier name or a raw model id.

        Returns a cached result for free when available; otherwise checks the
        budget, calls the model, charges the cost, and caches the response.
        """
        model_id = self.config.resolve_model(model)
        key = cache_mod.make_key(model_id, messages, temperature)

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
        resp = fn(model=model_id, messages=messages, temperature=temperature)
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
