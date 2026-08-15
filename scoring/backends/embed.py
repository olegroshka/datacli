"""Embedding backend: one vector per article through the shared ``llm`` layer.

The wide tier of design D: embed everything once (local Ollama embedding model
by default), then define categories cheaply on top of the vectors. Output kind is
``vector`` -- the store writes it under ``news/embeddings/<model>/`` rather than
into a schema sidecar. The schema still decides *what text* is embedded
(``text`` / ``max_chars``), so an embedding run is reproducible per schema.
"""

from __future__ import annotations

import time

from llm import tiers
from llm.models import LLM, BudgetExceeded
from scoring.backends.base import Estimate, Item, Result, sanitize_model_id
from scoring.backends.llm import LocalOnlyError
from scoring.config import ScoringConfig
from scoring.schema import Schema

LOCAL_SECONDS_PER_ITEM = 0.05


class EmbedBackend:
    kind = "vector"

    def __init__(
        self,
        config: ScoringConfig | None = None,
        *,
        model: str | None = None,
        llm: LLM | None = None,
        batch_size: int | None = None,
        max_chars: int | None = None,
        budget_usd: float | None = None,
    ) -> None:
        self.config = config or ScoringConfig()
        self.model = self.config.resolve_model(model or self.config.embed_model)
        budget = self.config.budget_usd if budget_usd is None else budget_usd
        if budget <= 0 and not tiers.is_local(self.model):
            raise LocalOnlyError(
                f"embedding model {self.model!r} is not local and the run is "
                "local-only (budget 0). Pass --budget-usd N or pick 'embed-local'."
            )
        self.llm = llm or LLM(
            models=self.config.models,
            cache_dir=self.config.cache_dir,
            budget_usd=(None if budget <= 0 else budget),
        )
        self.batch_size = batch_size or self.config.batch_size
        self.max_chars = max_chars  # None -> schema.max_chars
        self.id = "embed__" + sanitize_model_id(self.model.split("/", 1)[-1])

    def estimate(self, items: list[Item], schema: Schema) -> Estimate:
        n = len(items)
        return Estimate(
            n,
            seconds=n * LOCAL_SECONDS_PER_ITEM,
            cost_usd=0.0,
            note=(
                "local embedding model"
                if tiers.is_local(self.model)
                else "API embeddings"
            ),
        )

    def score(self, items: list[Item], schema: Schema) -> list[Result]:
        out: list[Result] = []
        limit = self.max_chars or schema.max_chars
        for start in range(0, len(items), self.batch_size):
            batch = items[start : start + self.batch_size]
            texts = [it.text(schema.text, limit) for it in batch]
            t0 = time.perf_counter()
            try:
                emb = self.llm.embed(texts, model=self.model)
                vectors = emb.vectors
                err = ""
            except BudgetExceeded as exc:
                vectors, err = [], f"budget: {exc}"
            except Exception as exc:
                vectors, err = [], f"{type(exc).__name__}: {str(exc)[:200]}"
            elapsed = time.perf_counter() - t0
            for i, it in enumerate(batch):
                vec = vectors[i] if i < len(vectors) else None
                out.append(
                    Result(
                        it.article_id,
                        it.date,
                        "ok" if vec else "error",
                        vector=vec,
                        model=self.model,
                        prompt_hash=f"text={schema.text};max_chars={limit}",
                        seconds=elapsed / max(len(batch), 1),
                        error=err if not vec else "",
                        cached=bool(vec) and emb.cached if not err else False,
                    )
                )
        return out
