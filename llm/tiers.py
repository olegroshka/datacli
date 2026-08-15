"""Model tiers: friendly names -> concrete LiteLLM model ids.

Callers (lab personas, scoring backends) pick a tier by name; ``datacli.toml``
``[lab.models]`` overrides any entry. ``local`` targets a 12GB GPU via Ollama;
step up to ``qwen2.5-coder:14b`` (~9GB Q4) for more quality, or a 32b model on a
24GB+ card. Embedding tiers point at Ollama embedding models (pull them once:
``ollama pull nomic-embed-text``).
"""

from __future__ import annotations

from typing import Mapping

DEFAULT_MODELS: dict[str, str] = {
    "local": "ollama/qwen2.5-coder:7b",
    "cheap": "openai/gpt-4o-mini",
    "mid": "anthropic/claude-sonnet-5",
    "strong": "anthropic/claude-opus-4-8",
    # embeddings
    "embed-local": "ollama/nomic-embed-text",
    "embed-cheap": "openai/text-embedding-3-small",
}


def resolve(tier_or_id: str, models: Mapping[str, str] | None = None) -> str:
    """Map a tier name (``"mid"``) to its model id, or pass an id through."""
    table = dict(DEFAULT_MODELS)
    if models:
        table.update(models)
    return table.get(tier_or_id, tier_or_id)


def is_local(model_id: str) -> bool:
    """``True`` for models served by Ollama (no API key, no network cost)."""
    return model_id.startswith("ollama/") or model_id.startswith("ollama_chat/")
