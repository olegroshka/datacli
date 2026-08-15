"""Model access for the lab -- a thin adapter over the shared ``llm`` package.

The provider-agnostic layer (LiteLLM, budget, cache, injectable functions) lives
in ``llm/models.py`` so other parts of datacli (news scoring) can use it without
the lab. This module keeps the lab's historical entry point: ``LLM(config, ...)``
built from a :class:`lab.config.LabConfig`.
"""

from __future__ import annotations

from typing import Any, Callable

from lab import cache as cache_mod
from lab.config import LabConfig
from llm.models import LLM as _SharedLLM
from llm.models import (  # noqa: F401  (compat)
    BudgetExceeded,
    CompletionFn,
    SpendTracker,
    _extract_text,
    _extract_usage,
)

__all__ = ["LLM", "BudgetExceeded", "SpendTracker", "CompletionFn"]


class LLM(_SharedLLM):
    """The lab's model handle: tiers, cache dir and budget come from ``[lab]``."""

    def __init__(
        self,
        config: LabConfig,
        *,
        completion_fn: CompletionFn | None = None,
        cost_fn: Callable[[Any], float] | None = None,
        cache: cache_mod.ResponseCache | None = None,
    ) -> None:
        super().__init__(
            models=config.models,
            cache_dir=config.cache_dir,
            budget_usd=config.budget.per_session_usd,
            warn_usd=config.budget.warn_usd,
            completion_fn=completion_fn,
            cost_fn=cost_fn,
            cache=cache,
        )
        self.config = config
