# News Scoring — Design Request (draft for brainstorm)

**Status:** BUILT — `event@2` 90-day local pass running (§10)  
**Created:** 2026-08-15  
**Depends on:** the `news` corpus (`EODHD_NEWS_SENTIMENT_FINDINGS.md`), roadmap item 3
(`NEWS_ROADMAP.md`)  
**Process:** this file is the *request*. We brainstorm against it, record the agreed
design in a new §"Decision" (and strike the rejected options), then build.

---

## 1. What we want (requirements)

- **R1 Own signals over the full text.** The vendor's per-article `polarity` is a
  coarse VADER-style score (median 0.98 on AAPL). We want scores *we* define,
  computed from `title + content`, per article and — where it matters — **per
  symbol within an article** (an article tagging 10 companies is not equally
  positive for all of them).
- **R2 Pluggable model backend.** The thing that turns text into scores must be a
  plugin behind one interface: a local model (Ollama), an API model (Anthropic /
  OpenAI via the lab's LiteLLM layer), a classical/HF classifier (FinBERT-style),
  an embedding model, and the vendor score itself as the trivial baseline. Swapping
  the model must not change the pipeline, the storage layout or the consumers.
- **R3 Flexible, dynamic categories.** What we score is *data, not code*: a
  declarative scoring **schema** (a file, like personas) that lists the categories
  and their value types — e.g. `sentiment: float[-1,1]`, `confidence: float[0,1]`,
  `event_type: enum{earnings, guidance, m&a, regulatory, product, macro, other}`,
  `direction_for_symbol: enum{up, down, neutral, n/a}`, `materiality: int[0..3]`,
  `horizon: enum{intraday, weeks, quarters}`, `novelty: bool`. New categories, new
  enum members, new schemas can be added later; old scores stay valid under their
  own schema version.
- **R4 Sidecars, never mutation.** Outputs are `article_id`-keyed tables written
  next to the corpus; the raw corpus is never rewritten. Every row carries the
  provenance to reproduce it: schema name + version, backend id + model id, prompt
  hash, temperature, scored_at, token counts / cost.
- **R5 Targeting and cost control.** Nobody scores 4.5 M articles with an API
  model by accident. Runs select a subset (date window, universe filter, ≤ N
  symbols, sampling), print a plan with a cost estimate, honour a budget, cache
  identical calls, and resume after interruption (day-keyed state like the crawl).
- **R6 Tiered by default.** Cheap-and-wide where volume matters, expensive-and-
  narrow where quality matters — the design must make "classifier/embeddings for
  everything, LLM for the targeted subset" natural, not a special case.
- **R7 Consumable.** DuckDB views per schema (`news_scores_<schema>`), joinable to
  `news`, `news_symbol_daily`, `prices` by `article_id` / `(date, ticker,
  exchange)`; the lab personas and MCP see them; `status` shows coverage
  (which days/articles are scored under which schema+backend).
- **R8 Evaluable.** Agreement with the vendor score, inter-backend agreement, and
  a small **gold set** (strong-model or human labels) to calibrate cheap backends
  before scaling them; the lab's skeptic pattern is available for spot checks.
- **R9 Local-first, offline-capable.** Everything runs from the CLI/shell on the
  local snapshot; API keys come from the environment; the local model path needs
  no network.

## 2. Facts that constrain the design

| fact | value | consequence |
|---|---|---|
| Corpus | 4.46 M articles, 2021→, ~4.7 k chars (~1.2 k tokens) each | scoring *everything* with an LLM is a five-figure-dollar / months-of-GPU job |
| Touches our universes | **2.26 M (51 %)**; last 365 d: **616 k** | the natural default target set |
| Symbols per article | 14 % none · 54 % 1–3 · 24 % 4–10 · 8 % >10 | per-symbol scoring is mostly cheap (≤ 3 symbols); the >10 tail needs a rule (skip / cap / article-level only) |
| Empty content | 0.14 % | skip |
| Re-publications | 0.1 % same link, later timestamp | score once per `article_id` (latest version) |
| Vendor score | VADER-style, saturated | baseline only |
| Existing plumbing | `lab/models.py` (LiteLLM, budget, injectable), `lab/cache.py` (model+messages hash), personas as TOML | reuse, don't reinvent |
| Machine | 12 GB GPU class (per README: `qwen2.5-coder:7b` fits) | local 7B LLM ≈ 3–6 s/article; HF classifier / embedder ≈ 50–200 articles/s |

### Cost model (order of magnitude; ~1.5 k input + 100 output tokens per article)

| backend | per article | 616 k (universe, 1 y) | 2.26 M (universe, all) | 4.46 M (all) |
|---|---|---|---|---|
| Vendor score (already have) | 0 | 0 | 0 | 0 |
| HF classifier / embedder, local GPU | ~10 ms | ~2 h | ~6 h | ~12 h |
| Local 7B LLM (Ollama) | ~4 s | ~1 month | ~3.5 months | ~7 months |
| API small (Haiku-class, ~$1/M in) | ~$0.002 | ~$1 k | ~$4 k | ~$8 k |
| API strong (Opus/Sonnet-class) | ~$0.02–0.05 | ~$15–30 k | — | — |

⇒ **An LLM pass is a targeted, tiered thing; only classifiers/embeddings can be
"everything".** Any design where new categories require re-running an LLM over the
corpus fails R3 in practice.

## 3. Vocabulary

- **Schema** — a named, versioned declaration of categories (fields with types,
  ranges, enums, descriptions, per-article vs per-symbol scope) plus the
  instructions/prompt a backend needs. File: `scoring/schemas/<name>.toml`.
- **Backend** — a plugin that turns `(article, schema)` into values: `llm`
  (LiteLLM: any provider), `hf` (transformers pipeline), `embed` (vector output),
  `vendor` (passthrough), later `ensemble`.
- **Run** — one (schema@version, backend@model, target selection) execution;
  resumable; writes sidecar partitions + a run state row per day.
- **Sidecar** — the score table for one schema@backend, partitioned by
  publication day like the corpus.

## 4. Design space — three candidate architectures

### A. LLM structured extraction, per article, schema-driven prompt

The schema is rendered into a JSON-schema / tool definition; one call per article
(or per article×symbol for per-symbol fields); the model returns the record;
validated, retried on invalid, cached.

+ best quality and full flexibility of categories · natural per-symbol stance ·
  every field explained by the model if asked
− every new category or schema version = a new pass over the text (cost, §2) ·
  local 7B is slow, small API models are the realistic tier · prompt drift across
  models makes cross-backend comparison noisy

### B. Classical / HF classifiers per category

Fine-tuned or off-the-shelf models (FinBERT sentiment, zero-shot NLI for
categories) run over everything on the GPU.

+ fast enough for the whole corpus, deterministic, cheap to re-run
− categories are baked into the model: a new category = a new model (or zero-shot
  NLI, which is weak on finance) · no per-symbol reasoning · numeric outputs only

### C. Embeddings as the durable substrate; categories as cheap downstream ops

Embed every article once (title + first N tokens; local model, versioned). New
categories are then *defined* against the embedding space: (i) zero-shot via
label descriptions, (ii) kNN from a small LLM-labelled seed set (a few thousand
articles), (iii) clustering + LLM labelling of centroids for discovery. Per-symbol
stance can use symbol-anchored passages (sentences mentioning the company).

+ "dynamic categories" becomes genuinely cheap: adding one is minutes, not a
  corpus pass · similarity search, dedup, clustering and retrieval come for free ·
  one embedding pass covers R6's "everything" tier
− accuracy of embedding-derived labels is below a strong LLM's · vector storage
  (4.5 M × 384–1024 floats ≈ 7–18 GB fp32; fp16/int8 halves/quarters it) ·
  per-symbol stance is approximate

### D (hybrid, my starting recommendation). C as substrate + A on the targeted tier + "extract rich once"

1. **Embed everything** (local, versioned) — the wide tier; enables C-style
   dynamic categories, retrieval, dedup.
2. **LLM-extract a rich record once** for the targeted tier (universe ∩ ≤ 3
   symbols ∩ date window, ~1–3 % of the corpus by default; expandable): not a
   narrow "sentiment" but an *event record* — entities/symbols, event type from
   an open vocabulary, direction/magnitude/horizon per symbol, key claim, quotes.
   New *categories* are then mostly **derived from the record** (mappings, rules,
   or embeddings over the record text) instead of re-reading 4.5 k chars of
   article; only genuinely new *questions* trigger a re-extraction.
3. **Classifier/vendor as baselines** for evaluation and as the fallback tier.
4. One **schema mechanism** covers all three: a schema declares fields; a
   backend declares which field types it can produce; the runner matches.

## 5. Proposed shape (to be confirmed in the brainstorm)

```
scoring/                        # new top-level package (provider-agnostic; lab may reuse it)
  schemas/*.toml                # categories: name, version, scope, fields, prompt, examples
  backends/{llm,hf,embed,vendor}.py   # Backend protocol: capabilities + score(batch, schema)
  select.py                     # targeting: window, universe, max_symbols, sample, budget
  runner.py                     # plan -> batches -> backend -> validate -> write -> state
  store.py                      # sidecar layout, provenance columns, run state, views
  evaluate.py                   # agreement vs vendor / other backends / gold set
  cli.py                        # score plan | run | status | eval   (+ shell `score`)
data root:
  news/scores/<schema>@<v>/<backend-id>/YYYY-MM-DD.parquet   # article_id, symbol?, fields…, provenance
  news/scores/<schema>@<v>/<backend-id>/state.csv            # per crawled day: n scored, cost, status
  news/embeddings/<model-id>/YYYY-MM-DD.parquet             # article_id, vector (fp16), model, dims
```

Sidecar rows are **wide per schema version** (one column per field; simple for
DuckDB/pandas consumers) plus fixed provenance columns; a `symbol` column is
NULL for article-level fields. Runs are keyed by publication day so `status`
can show coverage the same way it does for the crawl.

## 6. Open questions for the brainstorm (with a starting position)

1. **Architecture:** A, B, C or the hybrid D? — *D.*
2. **First schema:** narrow `sentiment_v1` (polarity, confidence, per-symbol
   direction) or the richer `event_v1` record from D.2? — *event_v1, with
   sentiment fields inside it; a narrow schema is a subset.*
3. **Default target set:** universe ∩ ≤ 3 symbols ∩ last 12 months (~150–300 k
   articles)? or last 90 days for a first pass? — *90 days first, then widen.*
4. **Default LLM tier:** local 7B (free, ~4 s/article, ~1 week for 150 k) vs a
   small API model (~$300 for 150 k, hours)? — *small API model for the first
   pass with a hard budget; local for continuous top-up.*
5. **Where the model layer lives:** reuse `lab/models.py` in place (scoring
   imports lab) or lift the LiteLLM/budget/cache layer into a shared package the
   lab also imports? — *lift it; scoring must not depend on the optional lab
   extra.*
6. **Per-symbol handling:** score per symbol only when ≤ 3 symbols; article-level
   otherwise (with `symbol = NULL`)? — *yes; the >10 tail is index/ETF wrap-ups.*
7. **Embeddings:** which local model (bge-small / e5-small at 384 dims fp16 ≈ 3.4
   GB for 4.5 M) and how much text (title + first 512 tokens)? — *e5-small-v2
   or bge-small-en-v1.5, title + lead 512 tokens, fp16.*
8. **Storage of dynamic/derived categories:** materialised sidecars (one per
   derived schema) or DuckDB views over the rich record + embeddings? — *views
   first; materialise only when a consumer needs speed.*
9. **Gold set:** 500–1,000 articles labelled by a strong model (Opus) once, plus
   a human spot-check, used to calibrate every cheaper backend? — *yes, first
   deliverable of the evaluation piece.*
10. **Integration:** a `score` command in the shell / `eodhd/cli.py`, and a
    `refresh` post-step for the top-up? — *`score` command; top-up wired into
    refresh only after the first full pass exists.*
11. **Registry:** are score sidecars datasets in the `news` lane (kind
    `news_scores`, day-keyed, partitioned) so `status` shows them? — *yes.*

## 7. Non-goals (for now)

Real-time streaming; training our own models; alerts; anything that writes back
to EODHD.

## 8. Decision (2026-08-15)

Agreed in the brainstorm; the rest of the open questions take the starting
positions in §6 unless noted.

- **Architecture: D** — embeddings as the wide substrate, an LLM-extracted rich
  record on the targeted tier, vendor/classifier as baselines, one schema
  mechanism for all backends.
- **First schema: `event_v1`** — the rich event record; sentiment fields are part
  of it, so a narrow sentiment schema is a projection, not a separate pass.
- **Local-only first.** The first tier runs entirely on the local machine: the LLM
  backend targets Ollama (default `local` tier = `ollama/qwen2.5-coder:7b`, the
  model already installed; swap via config), embeddings via a local Ollama
  embedding model. API models stay available as backends but are opt-in per run
  with an explicit budget. Consequence: the first targeted pass is small (recent
  window, universe ∩ ≤ 3 symbols) and grows as a continuous top-up.
- **Lift the model layer.** `lab/models.py`, `lab/cache.py`, the completion/usage
  types and the tier map move to a shared, dependency-light `llm/` package;
  `lab` imports it (thin compatibility shims keep the old import paths working);
  `scoring` imports it and does **not** depend on the `lab` extra.
- Q3 default window: last 90 days first · Q6 per-symbol only when ≤ 3 symbols ·
  Q7 embeddings: local Ollama embedding model, title + lead text · Q8 derived
  categories as views first · Q9 gold set is the first evaluation deliverable ·
  Q10 `score` command (refresh top-up later) · Q11 score sidecars are datasets in
  the `news` lane.

Build order: (1) `llm/` lift with lab shims → (2) `scoring/` substrate (schema
loader + `event_v1`, backends `vendor`/`llm`/`embed`, select, store, runner,
cli, tests with an injected completion) → (3) integration (registry, DuckDB
views, shell `score`, docs) and a real local smoke run to measure quality and
seconds/article on `qwen2.5-coder:7b`.

## 9. What was built (substrate, 2026-08-15)

```
llm/                          shared model layer (lifted from the lab): LLM.complete/embed, budget, cache, tiers
scoring/
  schemas/event_v1.toml       the rich event record (7 article fields + 3 per-symbol fields, prompt, rules)
  schema.py                   TOML -> Schema: prompt rendering, JSON shape, coerce/clamp/validate
  config.py                   [scoring] in datacli.toml (llm_model, embed_model, budget_usd=0 -> local-only, ...)
  backends/{vendor,llm,embed} vendor baseline · JSON-mode LLM via llm/ (repair turn, validation) · embeddings
  select.py                   per-day selection: latest version per article_id, content >= 200 chars,
                              universe filter (prices_state), target_symbols = tags ∩ universe if <= max_symbols
  store.py                    news/scores/<schema>@<v>/<backend>/<day>.parquet (wide, provenance columns),
                              news/embeddings/<model>/<day>.parquet, state.csv per day, upsert on (article_id, symbol)
  runner.py                   plan (free) / run (day by day, newest first, chunked writes, resumable, budget stop)
  cli.py                      score plan | run --run | status | schemas | backends   (shell: `score ...`)
```

- **Views:** `news_scores_<schema>` (latest version) and `news_scores_<schema>_v<N>`
  union every backend under that schema (`backend` / `model` columns tell them
  apart); `news_embeddings` unions every embedding model. Available in `sql`, the
  lab (schema context updated) and MCP.
- **Registry / status:** `news_scores` and `news_embeddings` are derived datasets
  in the `news` lane (no fetcher, no state) so `status news` shows rows and last
  day; `score status` gives the per-schema/backend breakdown.
- **Local-only guard:** `budget_usd = 0` (default) refuses any non-Ollama model at
  construction; `--budget-usd N` opens paid calls with a hard ceiling.
- **First smoke (6 articles, `qwen2.5-coder:7b`, JSON mode):** 6/6 valid records,
  ~2 s/article steady state (first call ~19 s incl. model load), sensible event
  types / summaries / per-symbol roles (a Coca-Cola tag on a Foods & Inns earnings
  article came back `peer`, `neutral`, relevance 0.1). Plan for the last 7 days:
  9,557 universe articles ≈ 5.3 h locally.

### How to run

```
uv run python -m scoring.cli plan --days 7                    # free: pending per day, est. time
uv run python -m scoring.cli run  --days 7 --limit 50 --run   # small local pass (~2 min)
uv run python -m scoring.cli run  --days 90 --run             # the first tier: resumable, newest days first
uv run python -m scoring.cli run  --backend vendor --days 90 --run     # baseline for agreement checks
uv run python -m scoring.cli status
uv run python eodhd/cli.py sql "SELECT event_type, count(*) FROM news_scores_event WHERE symbol IS NULL GROUP BY 1 ORDER BY 2 DESC"
```

### `score eval` and the first read (168 articles, 2026-08-15)

`score eval [--schema event] [--backend ID] [--compare A B]` prints health per
backend, our `sentiment` vs the vendor `polarity` (Pearson / Spearman / 3×3 sign
table with a ±0.05 dead band), the field distributions, and — with two backends
on the same articles — sentiment correlation, event_type / direction agreement
with Cohen's kappa, and materiality MAE.

First read on the running 90-day pass (`qwen2.5-coder:7b`, 168 articles, 0
invalid, 2.1 s/item):

| | ours | vendor |
|---|---|---|
| mean sentiment | 0.21 | 0.89 |
| share positive / negative | 59 % / 24 % | 95 % / 5 % |
| p10 / p50 / p90 | −0.5 / 0.5 / 0.5 | 0.89 / 0.996 / 0.999 |

Pearson 0.15, sign agreement 0.60 — the vendor calls almost everything positive;
ours disagrees on 36 articles it calls negative. Distributions: `event_type`
earnings 33 %, **other 40 %**; `materiality` **2 on 82 %** of articles; horizon
`n_a` 33 %; per-symbol role subject 70 % / peer 25 %, direction up 51 % / down
18 %. Calibration signals for an `event_v2` prompt once the pass has more data:
the model quantises sentiment to ±0.5 (ask for finer granularity / anchors),
`materiality` collapses onto 2 (give anchored examples per level), and 40 %
`other` suggests missing categories (index/ETF flows, dividends declared,
partnerships) or a stronger nudge to pick the closest class.

Next (roadmap item 3, continued): let the 90-day pass finish; gold set (needs a
paid model with an explicit `--budget-usd`, or hand labels) → `--compare`; the
embedding pass (`ollama pull nomic-embed-text` first); `event_v2` prompt from the
eval findings; a `refresh` post-step for the daily top-up.

## 10. `event_v2` (2026-08-16) and the running pass

Recalibrated from ~30k `event@1` records (§9 findings), same shape so v1/v2 rows
compare field by field:

| change | why | v1 → v2 on the same 40 articles |
|---|---|---|
| `event_type` gains `supply_chain`, `competition`, `partnership`, `economic_data`, `index_etf_flow`, `technical_analysis`; prompt says "closest class, never invent one" | 85 % of invalid records were invented classes; 40 % `other` | `other` 38 % → 10 %, 0 invalid |
| anchored seven-point sentiment scale (−1/−0.6/−0.3/0/+0.3/+0.6/+1), "use the whole range" | v1 quantised at ±0.5 | values now spread over −0.6…+0.6; corr(v1,v2) 0.75 |
| materiality anchored per level with a stated base rate ("most articles are 0 or 1") | 82 % landed on 2 | share of 2s 73 % → 57 % (still no 0s on company-specific articles) |
| `horizon`: `n_a` only when nothing is price-relevant | 33 % `n_a` | inconclusive on 40 articles (watch at scale) |

Also landed with it: repair turns on validation problems (enum drift, missing
fields) and a `max_tokens` cap; the earlier invalid rows were re-scored (all
rescued on the swept days).

**Pass:** the v1 pass was stopped at 17/90 days (~35k articles, kept on disk as
`event@1`) and restarted as `event@2` over the same 90-day window (134k
articles, ~2.3 s/article). `news_scores_event` now points at v2;
`news_scores_event_v1` remains for comparison (`SELECT … FROM
news_scores_event_v1 JOIN news_scores_event_v2 USING (article_id)`).

Next: eval at scale when the pass lands (`score eval`, v1-vs-v2 join); embedding
pass; gold set; the daily top-up as a `refresh` post-step.
