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

# Reasoning tiers that only accept temperature=1 (they reject temperature=0). We
# route grounded steps AROUND these so temperature=0 is honoured and the evidence
# is reproducible; they are used only for the FINAL synthesis (`review_model`),
# where non-determinism in the prose is acceptable.
_TEMP1_PREFIX = ("o1", "o3", "o4", "gpt-5")
_TEMP1_CONTAINS = ("claude-opus-4", "claude-sonnet-5")


def honors_temperature_zero(model_id: str) -> bool:
    """True if the model accepts ``temperature=0`` (i.e. can run deterministically).

    Grounded reasoning must run on a model that honours ``temperature=0``; the
    strong reasoning tiers that force ``temperature=1`` belong in ``review_model``.
    """
    mid = model_id.split("/", 1)[-1].lower()
    if any(mid.startswith(prefix) for prefix in _TEMP1_PREFIX):
        return False
    return not any(token in mid for token in _TEMP1_CONTAINS)


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
    allow_python: bool = False  # opt-in restricted code executor (off by default)
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
        allow_python=bool(section.get("allow_python", False)),
        budget=budget,
        models=models,
    )
