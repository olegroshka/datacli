"""Response cache -- re-exported from the shared ``llm`` package.

Kept so ``from lab import cache`` / ``lab.cache.ResponseCache`` keep working; the
implementation lives in ``llm/cache.py``.
"""

from __future__ import annotations

from llm.cache import ResponseCache, make_key

__all__ = ["ResponseCache", "make_key"]
