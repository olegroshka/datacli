"""Value types for model calls. Pure dataclasses, no dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field


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
    """The result of a single chat completion."""

    text: str
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cached: bool = False


@dataclass(frozen=True)
class Embedding:
    """The result of embedding one batch of texts (one vector per input)."""

    vectors: list[list[float]]
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    cached: bool = False

    @property
    def dims(self) -> int:
        return len(self.vectors[0]) if self.vectors else 0
