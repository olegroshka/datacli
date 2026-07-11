"""Core value types for the lab. Kept dependency-free and pure.

The ``Finding`` is the atomic, auditable unit of the grounding contract: a claim
is only valid alongside the exact query that produced it and that query's result.
Rendering lives elsewhere so this module stays importable without Rich.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Usage:
    """Token accounting for one model call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class Completion:
    """The result of a single model call."""

    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cached: bool = False


@dataclass(frozen=True)
class Finding:
    """A grounded claim: the statement, the query that produced it, and the rows.

    Nothing in ``claim`` may reference a number that is not present in ``rows`` --
    that invariant is the whole point of the lab. ``provenance`` carries the
    reproducibility context (persona, model, data root, schema version, whether the
    result came from cache, and token/cost usage).
    """

    claim: str
    sql: str
    columns: list[str] = field(default_factory=list)
    rows: list[tuple[Any, ...]] = field(default_factory=list)
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)
