"""LLM backend: schema-driven JSON extraction through the shared ``llm`` layer.

One completion per article (JSON mode where the provider supports it), the
schema rendered into the prompt, the answer parsed, coerced and validated by the
schema, up to ``max_repairs`` repair turns that show the model its own answer and
the concrete problems (unparseable JSON, a value outside an enum, a missing
field or symbol block), a ``max_tokens`` cap against runaway generations, and
everything cached and budgeted by ``llm.models.LLM``. Local (Ollama) by default; an API model is refused unless
the run has a non-zero budget (design §8: local-only first).
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

from llm import tiers
from llm.models import LLM, BudgetExceeded
from scoring.backends.base import Estimate, Item, Result, sanitize_model_id
from scoring.config import ScoringConfig
from scoring.schema import Schema

#: Rough wall-clock per article on a 12 GB-class GPU with a 7B model (measured
#: on the first smoke run; overridden by ``seconds_per_item``).
LOCAL_SECONDS_PER_ITEM = 4.0
_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


class LocalOnlyError(RuntimeError):
    """An API model was requested while the run is local-only (budget 0)."""


class LLMBackend:
    kind = "record"

    def __init__(
        self,
        config: ScoringConfig | None = None,
        *,
        model: str | None = None,
        llm: LLM | None = None,
        temperature: float = 0.0,
        max_repairs: int = 2,
        seconds_per_item: float | None = None,
        budget_usd: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self.config = config or ScoringConfig()
        self.model = self.config.resolve_model(model or self.config.llm_model)
        budget = self.config.budget_usd if budget_usd is None else budget_usd
        if budget <= 0 and not tiers.is_local(self.model):
            raise LocalOnlyError(
                f"model {self.model!r} is not local and the run is local-only "
                "(budget 0). Pass --budget-usd N (or set [scoring].budget_usd) to "
                "allow paid calls, or pick a local tier such as 'local'."
            )
        self.llm = llm or LLM(
            models=self.config.models,
            cache_dir=self.config.cache_dir,
            budget_usd=(None if budget <= 0 else budget),
        )
        self.temperature = temperature
        self.max_repairs = max_repairs
        # caps runaway generations (seen: 81,920 completion tokens on one article)
        self.max_tokens = (
            max_tokens if max_tokens is not None else self.config.max_tokens
        )
        self.seconds_per_item = (
            seconds_per_item
            if seconds_per_item is not None
            else (LOCAL_SECONDS_PER_ITEM if tiers.is_local(self.model) else 1.5)
        )
        self.id = sanitize_model_id(self.model)

    # -- planning ------------------------------------------------------------ #
    def estimate(self, items: list[Item], schema: Schema) -> Estimate:
        n = len(items)
        note = "local model: no API cost" if tiers.is_local(self.model) else ""
        cost = 0.0
        if not tiers.is_local(self.model):
            in_tokens = sum(
                len(it.text(schema.text, schema.max_chars)) // 4 + 600 for it in items
            )
            out_tokens = 250 * n
            cost = _api_cost(self.model, in_tokens, out_tokens)
            note = f"~{in_tokens/1e6:.2f}M in / {out_tokens/1e6:.2f}M out tokens"
        return Estimate(n, seconds=n * self.seconds_per_item, cost_usd=cost, note=note)

    # -- prompt -------------------------------------------------------------- #
    def build_messages(self, item: Item, schema: Schema) -> list[dict[str, str]]:
        symbols = list(item.target_symbols) if schema.per_symbol else []
        shape_hint = {"article": {f.name: "..." for f in schema.fields}}
        if symbols:
            shape_hint["symbols"] = {
                s: {f.name: "..." for f in schema.symbol_fields} for s in symbols
            }
        parts = [
            schema.instructions,
            "",
            schema.field_spec(),
            "",
            (
                "Target symbols for the per-symbol fields: " + ", ".join(symbols)
                if symbols
                else 'There are no target symbols; return "symbols": {}.'
            ),
            "",
            "Return exactly this JSON shape:",
            json.dumps(shape_hint, ensure_ascii=False),
            "",
            "ARTICLE:",
            item.text(schema.text, schema.max_chars),
        ]
        messages = []
        if schema.system:
            messages.append({"role": "system", "content": schema.system})
        messages.append({"role": "user", "content": "\n".join(parts)})
        return messages

    # -- scoring ------------------------------------------------------------- #
    def score(self, items: list[Item], schema: Schema) -> list[Result]:
        return [self._score_one(it, schema) for it in items]

    def _score_one(self, item: Item, schema: Schema) -> Result:
        symbols = list(item.target_symbols) if schema.per_symbol else []
        messages = self.build_messages(item, schema)
        res = Result(
            item.article_id,
            item.date,
            "error",
            model=self.model,
            prompt_hash=schema.prompt_hash(),
            temperature=self.temperature,
        )
        t0 = time.perf_counter()
        raw = ""
        best: tuple[dict, dict, list[str]] | None = None  # (article, symbols, problems)
        for attempt in range(self.max_repairs + 1):
            try:
                comp = self.llm.complete(
                    messages,
                    model=self.model,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                    max_tokens=self.max_tokens,
                )
            except BudgetExceeded as exc:
                res.error = f"budget: {exc}"
                res.seconds = time.perf_counter() - t0
                return res
            except Exception as exc:  # provider / network / decode
                res.error = f"{type(exc).__name__}: {str(exc)[:200]}"
                res.seconds = time.perf_counter() - t0
                return res
            res.prompt_tokens += comp.usage.prompt_tokens
            res.completion_tokens += comp.usage.completion_tokens
            res.cost_usd += comp.cost_usd
            res.cached = res.cached or comp.cached
            raw = comp.text
            payload = parse_json(raw)
            if payload is None:
                complaint = (
                    "That was not a single valid JSON object. Return only the JSON "
                    "object in the required shape, with no prose."
                )
                problems = ["unparseable JSON"]
            else:
                article, per_symbol, problems = schema.validate(payload, symbols)
                # keep the most complete attempt so far
                if best is None or _n_missing(article, schema) < _n_missing(
                    best[0], schema
                ):
                    best = (article, per_symbol, problems)
                if not problems:
                    break
                complaint = (
                    "Your JSON had these problems: "
                    + "; ".join(problems[:12])
                    + ". Fix them and return the COMPLETE object again (every article "
                    "field and every listed symbol), using only the allowed values:"
                    + "\n"
                    + schema.field_spec()
                )
            if attempt >= self.max_repairs:
                break
            # repair turn: show the model its own answer and what was wrong
            messages = messages + [
                {"role": "assistant", "content": raw[:4000]},
                {"role": "user", "content": complaint},
            ]
        res.seconds = time.perf_counter() - t0
        res.raw = raw
        if best is None:
            res.status = "invalid"
            res.problems = ["unparseable JSON"]
            return res
        article, per_symbol, problems = best
        res.article, res.symbols, res.problems = article, per_symbol, problems
        missing_article = [f for f in schema.field_names() if article.get(f) is None]
        res.status = "invalid" if missing_article else "ok"
        return res


def _n_missing(article: dict, schema: Schema) -> int:
    return sum(1 for f in schema.field_names() if article.get(f) is None)


def parse_json(text: str) -> Any:
    """Best-effort JSON extraction: strip code fences, take the outermost {...}."""
    if not text:
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z]*\s*", "", candidate).rstrip("`").strip()
    try:
        return json.loads(candidate)
    except ValueError:
        pass
    m = _JSON_BLOCK.search(candidate)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _api_cost(model_id: str, in_tokens: int, out_tokens: int) -> float:
    """Approximate USD for an API model via LiteLLM's price table (0 if unknown)."""
    try:
        import litellm  # type: ignore[import-not-found]

        provider_less = model_id.split("/", 1)[-1]
        info = litellm.model_cost.get(model_id) or litellm.model_cost.get(provider_less)
        if not info:
            return 0.0
        return in_tokens * float(
            info.get("input_cost_per_token", 0.0)
        ) + out_tokens * float(info.get("output_cost_per_token", 0.0))
    except Exception:
        return 0.0
