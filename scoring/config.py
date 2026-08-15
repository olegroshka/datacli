"""``[scoring]`` configuration from ``datacli.toml`` (merged over defaults).

Keys (all optional)::

    [scoring]
    llm_model    = "local"              # tier or model id for the llm backend
    embed_model  = "embed-local"        # tier or model id for the embed backend
    cache_dir    = ".llm_cache"         # response cache (relative to the repo)
    budget_usd   = 0.0                  # hard per-run ceiling; 0 = local-only (any paid call refused)
    max_symbols  = 3                    # per-symbol scoring only up to this many target symbols
    window_days  = 90                   # default target window for `score plan/run`
    batch_size   = 16                   # embed batch size

Model tiers come from ``[lab.models]`` (shared with the lab; see ``llm.tiers``).
API keys never live here -- they come from the environment.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from llm import tiers

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "datacli.toml"


@dataclass(frozen=True)
class ScoringConfig:
    llm_model: str = "local"
    embed_model: str = "embed-local"
    cache_dir: Path = REPO_ROOT / ".llm_cache"
    budget_usd: float = 0.0
    max_symbols: int = 3
    window_days: int = 90
    batch_size: int = 16
    models: dict[str, str] = field(default_factory=lambda: dict(tiers.DEFAULT_MODELS))

    def resolve_model(self, tier_or_id: str) -> str:
        return tiers.resolve(tier_or_id, self.models)

    @property
    def local_only(self) -> bool:
        """A zero budget means: refuse any call that could cost money."""
        return self.budget_usd <= 0


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}


def load(path: Path = CONFIG_PATH) -> ScoringConfig:
    data = _read(path)
    section = data.get("scoring") or {}
    if not isinstance(section, dict):
        section = {}
    models = dict(tiers.DEFAULT_MODELS)
    lab_models = (
        (data.get("lab") or {}).get("models")
        if isinstance(data.get("lab"), dict)
        else None
    )
    if isinstance(lab_models, dict):
        models.update({str(k): str(v) for k, v in lab_models.items()})
    if isinstance(section.get("models"), dict):
        models.update({str(k): str(v) for k, v in section["models"].items()})
    cache_dir = section.get("cache_dir")
    return ScoringConfig(
        llm_model=str(section.get("llm_model", "local")),
        embed_model=str(section.get("embed_model", "embed-local")),
        cache_dir=(REPO_ROOT / cache_dir) if cache_dir else REPO_ROOT / ".llm_cache",
        budget_usd=float(section.get("budget_usd", 0.0) or 0.0),
        max_symbols=int(section.get("max_symbols", 3)),
        window_days=int(section.get("window_days", 90)),
        batch_size=int(section.get("batch_size", 16)),
        models=models,
    )
