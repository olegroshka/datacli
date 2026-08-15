"""Shared, dependency-light model access: LiteLLM behind one interface, with a
spend budget and an on-disk response cache.

Lifted out of the Raw Data Lab so that any part of datacli (the lab, the news
scoring layer, future tools) talks to models the same way. Nothing here imports
LiteLLM at module load -- it is bound lazily on the first real, uncached call --
so importing this package never requires the ``lab`` extra.

Modules:
    types   Usage / Completion / Embedding value types (pure dataclasses)
    cache   ResponseCache: sha256(model, messages, temperature) -> JSON on disk
    tiers   DEFAULT_MODELS tier map (local / cheap / mid / strong) + resolver
    models  LLM: complete() / embed() with budget, cache, injectable functions
"""

from __future__ import annotations

__all__ = ["types", "cache", "tiers", "models"]
