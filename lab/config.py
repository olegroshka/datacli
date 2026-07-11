"""``[lab]`` configuration: model tiers, budget, cache, default persona.

Read from the repo-local ``datacli.toml`` (the same file the eodhd source uses),
merged over built-in defaults so the lab is usable with zero config. API keys are
never stored here -- they come from the environment (``ANTHROPIC_API_KEY``,
``OPENAI_API_KEY``, ...).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "datacli.toml"

# Model tiers -> concrete LiteLLM model ids. Personas pick a tier by name.
# `local` targets a 12GB GPU via Ollama; step up to qwen2.5-coder:14b (~9GB Q4)
# for more quality, or qwen2.5-coder:32b on a 24GB+ card.
DEFAULT_MODELS: dict[str, str] = {
    "local": "ollama/qwen2.5-coder:7b",
    "cheap": "openai/gpt-4o-mini",
    "mid": "anthropic/claude-sonnet-5",
    "strong": "anthropic/claude-opus-4-8",
}


@dataclass(frozen=True)
class Budget:
    """Per-session spend guardrails (USD). ``None`` means unbounded."""

    per_session_usd: float | None = 1.00
    warn_usd: float | None = 0.50


@dataclass(frozen=True)
class LabConfig:
    default_persona: str = "analyst"
    cache_dir: Path = REPO_ROOT / ".lab_cache"
    reports_dir: Path = REPO_ROOT / "lab_reports"
    budget: Budget = field(default_factory=Budget)
    models: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_MODELS))

    def resolve_model(self, tier_or_id: str) -> str:
        """Map a tier name (``"mid"``) to its model id, or pass an id through."""
        return self.models.get(tier_or_id, tier_or_id)


def _read_lab_section(path: Path = CONFIG_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get("lab", {})
    return section if isinstance(section, dict) else {}


def load(path: Path = CONFIG_PATH) -> LabConfig:
    """Load ``[lab]`` config from ``datacli.toml`` merged over the defaults."""
    section = _read_lab_section(path)

    models = dict(DEFAULT_MODELS)
    if isinstance(section.get("models"), dict):
        models.update({str(k): str(v) for k, v in section["models"].items()})

    budget_raw = section.get("budget", {})
    budget = Budget(
        per_session_usd=budget_raw.get("per_session_usd", Budget.per_session_usd),
        warn_usd=budget_raw.get("warn_usd", Budget.warn_usd),
    )

    cache_dir = section.get("cache_dir")
    cache_path = (REPO_ROOT / cache_dir) if cache_dir else (REPO_ROOT / ".lab_cache")
    reports_dir = section.get("reports_dir")
    reports_path = (
        (REPO_ROOT / reports_dir) if reports_dir else (REPO_ROOT / "lab_reports")
    )

    return LabConfig(
        default_persona=str(section.get("default_persona", "analyst")),
        cache_dir=cache_path,
        reports_dir=reports_path,
        budget=budget,
        models=models,
    )
