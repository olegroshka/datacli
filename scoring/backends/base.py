"""The backend contract and the value objects that cross it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from scoring.schema import Schema


@dataclass(frozen=True)
class Item:
    """One article to score, as selected from the corpus."""

    article_id: str
    date: str  # YYYY-MM-DD publication day (partition key)
    title: str
    content: str
    symbols: tuple[str, ...]  # every vendor symbol tag
    target_symbols: tuple[str, ...]  # the ones we score per-symbol (<= max_symbols)
    vendor_polarity: float | None = None
    vendor_pos: float | None = None
    vendor_neg: float | None = None
    source: str | None = None

    def text(self, mode: str, max_chars: int) -> str:
        """The text a backend sees, per the schema's ``text``/``max_chars``."""
        if mode == "title":
            body = self.title or ""
        elif mode == "content":
            body = self.content or ""
        else:
            body = f"{self.title or ''}\n\n{self.content or ''}".strip()
        if max_chars and len(body) > max_chars:
            body = body[:max_chars].rstrip() + " [...]"
        return body


@dataclass
class Result:
    """What a backend produced for one item.

    ``status``: ``ok`` (record usable), ``invalid`` (backend answered but the
    record failed validation), ``error`` (call failed), ``skipped``.
    """

    article_id: str
    date: str
    status: str
    article: dict[str, Any] = field(default_factory=dict)
    symbols: dict[str, dict[str, Any]] = field(default_factory=dict)
    vector: list[float] | None = None
    model: str = ""
    prompt_hash: str = ""
    temperature: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    seconds: float = 0.0
    problems: list[str] = field(default_factory=list)
    error: str = ""
    raw: str = ""  # raw model text (kept for debugging invalid records)


@dataclass(frozen=True)
class Estimate:
    """Planning numbers for a batch of items."""

    n_items: int
    seconds: float
    cost_usd: float
    note: str = ""


class Backend(Protocol):
    """A pluggable scorer."""

    id: str  # e.g. "vendor", "ollama__qwen2.5-coder_7b", "embed__nomic-embed-text"
    kind: str  # "record" | "vector"
    model: str  # resolved model id or "" for non-model backends

    def estimate(self, items: list[Item], schema: Schema) -> Estimate: ...

    def score(self, items: list[Item], schema: Schema) -> list[Result]: ...


def sanitize_model_id(model_id: str) -> str:
    """``ollama/qwen2.5-coder:7b`` -> ``ollama__qwen2.5-coder_7b`` (path-safe)."""
    out = []
    for ch in model_id:
        if ch.isalnum() or ch in "._-":
            out.append(ch)
        elif ch == "/":
            out.append("__")
        else:
            out.append("_")
    return "".join(out) or "model"
