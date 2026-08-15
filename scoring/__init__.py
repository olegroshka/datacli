"""News scoring: schema-driven, backend-pluggable scores over the news corpus.

Design: ``eodhd/NEWS_SCORING_DESIGN.md`` (decision §8). In one line: a *schema*
(``scoring/schemas/<name>.toml``) declares the categories; a *backend* (local
LLM, API LLM, vendor baseline, embeddings) produces them; the *runner* selects a
target set, scores it day by day (resumable, budgeted, cached) and writes
``article_id``-keyed sidecars under ``<data-root>/news/scores/`` -- the raw
corpus is never touched.

Modules:
    schema    load/validate a scoring schema, coerce+validate model output
    config    ``[scoring]`` section of datacli.toml (+ ``[lab.models]`` tiers)
    select    target selection from the DuckDB ``news`` view
    store     sidecar layout, provenance columns, per-day state
    runner    plan / run / status
    backends  vendor (baseline), llm (via ``llm``), embed (via ``llm``)
    cli       ``score plan | run | status | schemas | backends``
"""

from __future__ import annotations

__all__ = ["schema", "config", "select", "store", "runner", "backends", "cli"]
