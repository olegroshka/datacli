"""Core value types for the lab. Kept dependency-free and pure.

The ``Finding`` is the atomic, auditable unit of the grounding contract: a claim
is only valid alongside the exact query that produced it and that query's result.
Rendering lives elsewhere so this module stays importable without Rich.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Usage / Completion live in the shared ``llm`` package and are re-exported here
# so ``lab.types.Usage`` keeps working.
from llm.types import Completion, Usage  # noqa: E402,F401


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
