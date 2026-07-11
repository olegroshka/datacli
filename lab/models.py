"""Provider-agnostic model access with a budget and a response cache.

Wraps LiteLLM (one interface across Anthropic / OpenAI / Ollama) but takes the
completion + cost functions by injection, so the whole layer is unit-testable
without LiteLLM installed and without ever hitting a live API. LiteLLM is imported
lazily, only when a real call is made.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Protocol

from lab import cache as cache_mod
from lab.config import LabConfig, honors_temperature_zero
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
        self._temp_warned: set[str] = set()  # models we've warned about, once each

    # -- lazy LiteLLM bindings (only touched on a real, uncached call) -------- #
    @staticmethod
    def _litellm() -> Any:
        """Import LiteLLM and apply our global policy once.

        We send ``temperature=0`` for deterministic grounding, but some strong
        models (e.g. Opus 4.8 and other reasoning tiers) only accept
        ``temperature=1`` and LiteLLM raises ``UnsupportedParamsError`` otherwise.
        ``drop_params`` lets LiteLLM drop a parameter a given model can't honour
        instead of failing, so a strong-tier skeptic on Opus degrades to that
        model's mandated temperature rather than aborting the whole pipeline.
        """
        import litellm  # type: ignore[import-not-found]

        litellm.drop_params = True
        return litellm

    def _litellm_completion(
        self, *, model: str, messages: list[dict[str, Any]], temperature: float
    ) -> Any:
        litellm = self._litellm()
        return litellm.completion(
            model=model, messages=messages, temperature=temperature
        )

    def _litellm_cost(self, resp: Any) -> float:
        try:
            litellm = self._litellm()

            return float(litellm.completion_cost(completion_response=resp) or 0.0)
        except Exception:
            return 0.0

    def _should_warn_temperature(
        self, model_id: str, temperature: float, deterministic: bool
    ) -> bool:
        """A grounded step (deterministic) asked for temp<1 on a temp-1-only model."""
        return (
            deterministic
            and temperature < 1.0
            and not honors_temperature_zero(model_id)
            and model_id not in self._temp_warned
        )

    def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        model: str,
        temperature: float = 0.0,
        deterministic: bool = True,
    ) -> Completion:
        """Run one completion. ``model`` is a tier name or a raw model id.

        Returns a cached result for free when available; otherwise checks the
        budget, calls the model, charges the cost, and caches the response.

        ``deterministic`` marks a grounded step that expects ``temperature=0`` to be
        honoured; if the resolved model only supports ``temperature=1`` (a reasoning
        tier), we warn once so the misconfiguration is visible. Synthesis calls pass
        ``deterministic=False`` -- they knowingly use a strong temp-1 model.
        """
        model_id = self.config.resolve_model(model)
        # Real calls only (injected fakes in tests are trusted and stay quiet).
        if self._completion_fn is None and self._should_warn_temperature(
            model_id, temperature, deterministic
        ):
            self._temp_warned.add(model_id)
            print(
                f"[lab] note: {model_id} only supports temperature=1, so this "
                f"grounded step is not reproducible. Route grounded work to a "
                f"temperature-0 tier (local/cheap) and keep this model in "
                f"review_model for synthesis.",
                file=sys.stderr,
            )
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
