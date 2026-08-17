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

## 11. Choosing model and schema by measurement (`score bench`, 2026-08-17)

The v2 pass above was stopped mid-flight on a fair challenge: the incumbent model
was `ollama/qwen2.5-coder:7b` — a **code** model, inherited by accident. The
`local` tier in `llm/tiers.py` was picked for the lab's SQL generation, and
scoring resolved the same tier without anyone choosing it for financial text.
Rather than swap on intuition, `scoring/bench.py` + `score bench` compare
`(model × schema)` configs on **one fixed article sample**, so every difference
is the config and not the draw.

### What the bench measures

Four axes, because the cheap ones do not predict the one that matters:

1. **Validity** — `invalid_share`, `other_share`, `s_per_item`. Can it fill the
   schema at all, and how fast.
2. **Calibration** — `sent_distinct`, `sent_sd`, `mat_low_share`,
   `horizon_na_share`. Does it use the range, or park on one value.
3. **Return signal** — does `sentiment` order the realised move, does
   `materiality` order its magnitude. No ground truth needed.
4. **Head-to-head** — on articles where two configs point *opposite ways*,
   whose sign matches the move.

### Two metric traps, both of which produced a wrong answer first

These are worth recording because each one inverted a conclusion I had already
drawn, and neither was visible in the model outputs themselves.

**Trap 1 — scoring an abstention as a loss.** `llama3.1:8b` showed the best raw
return spread (388 bps) yet "lost" its head-to-head 2–47. It answers *neutral*
on 53.7 % of articles; those were being counted as wrong calls, and there was no
base-rate comparison to beat. Fixed with a `NEUTRAL_BAND` (|sentiment| ≤ 0.05 is
an abstention, not a call), `sent_coverage`, head-to-head restricted to
*committed* disagreements, and a hit rate stated against `r0_pos_base_rate` as
`sent_edge_pp`. Recomputing from the saved parquets reversed the round-1 verdict.

**Trap 2 — measuring the contaminated horizon.** The edge was computed only on
`r0`, the session the article lands in. But an article that says "shares fell
8 %" makes the direction trivially inferable — a high `r0` edge can be hindsight
rather than skill. Adding the same edge on `r1` (strictly *after* publication)
reordered the candidates rather than confirming them, so reporting one horizon
alone was again enough to pick the wrong model.

A corollary worth keeping: **calibration quality and directional skill are
largely independent.** `phi4:14b` is the best-behaved config on every cheap axis
— 0 % invalid, 0.8 % `other`, 1.2 % `horizon=n_a`, abstains least (86 %
coverage) — and still lands mid-pack on the contemporaneous edge. A model can
follow the schema beautifully and have no view worth anything. This is precisely
why the bench carries the return axis at all.

### Screening rounds (259 articles, all on `event@2`, same draw)

| config | s/item | invalid | other | neutral | coverage | edge r0 (pp) | edge r1 (pp) |
|---|---|---|---|---|---|---|---|
| qwen2.5-coder:7b *(incumbent)* | 0.91 | 0.4 % | 6.2 % | 21 % | 0.77 | **−1.0** | **+3.3** |
| qwen2.5:7b-instruct | 2.23 | 0 % | 1.2 % | 21 % | 0.75 | **+5.1** | **+1.5** |
| llama3.1:8b-instruct | 2.15 | 1.5 % | 4.7 % | 54 % | 0.41 | **−2.4** | **+1.7** |
| qwen2.5:14b-instruct | 3.71 | 0.4 % | 0.4 % | 23 % | 0.76 | **+5.9** | **+5.6** |
| phi4:14b | 4.24 | 0 % | 0.8 % | 11 % | 0.86 | **+0.9** | **+7.1** |

Reading this honestly: at n≈220 committed rows the standard error on an edge is
~3.4 pp, so **none of these separations is significant on its own**. The
screening earns two things only — the incumbent code model is the *only* config
negative on the contemporaneous edge (the user's instinct was right), and
`llama3.1`'s 41 % coverage disqualifies it regardless of edge, since it throws
away half the corpus. Head-to-head is uninformative at this size: with
abstentions correctly excluded there are just 1–7 committed sign disagreements
per pair, i.e. the models differ mainly in *when they abstain*, not in direction.
Materiality→|r1| is likewise noise here (monotone −0.8…+0.4) despite being clean
on 27k rows — a reminder that these screens cannot resolve the finer fields.

### The decisive run

One paired run over **1267 articles** (a 1500 request, capped by the
price-availability filter; ~1260 committed rows → SE ≈ 1.4 pp, so a 5 pp edge is
~3.5 SE) covering the model and schema questions together:

```
score bench --n 1500 --days 30 --seed 7 --run-id decisive --configs \
  'qwen2.5:14b-instruct-q4_K_M:event@2,qwen2.5:14b-instruct-q4_K_M:event@3,\
   phi4:14b:event@2,phi4:14b:event@3,\
   qwen2.5:7b-instruct:event@2,qwen2.5-coder:7b:event@2'
```

Both 14b finalists are run against **both** schemas so the model effect and the
schema effect are separable, with the incumbent kept as a control — if it is
genuinely worse, the ~30k rows already scored under it need redoing, and that is
a decision the control has to earn. Each config's parquet is written as it
completes, so a late failure costs only the tail.

**Decision rule, fixed before reading the results:** pick on `edge_r1` (the
predictive horizon) subject to `sent_coverage ≥ 0.6`, break ties by `s/item`,
and treat a schema as winning only if it improves *both* 14b models — a gain on
one is a model×schema interaction, not a schema improvement.

### `event_v3` — the schema under test

v2's own recalibration exposed the general failure: **an LLM copies whatever
numbers the prompt names.** v1 quantised at ±0.5; v2 named seven anchors and
98 % of answers landed exactly on them. Naming better numbers cannot fix this, so
v3 stops naming numbers at all — the model emits **ordinal labels** and code maps
them to floats via `numeric` / `numeric_as` in the TOML. Because `numeric_as`
reuses the old names, `sentiment` and `materiality` remain floats downstream and
every existing view, metric and query keeps working unchanged; the labels arrive
alongside as `sentiment_label` / `materiality_label`.

v3 also adds the two fields the r0/r1 split argued for:
`price_move_mentioned` (bool) to flag exactly the articles that make `r0`
hindsight, and `expectation_vs_outcome` (beat / in_line / miss / not_applicable)
to separate surprise from level — the thing that should carry *forward*.

### Amendment to the decision rule (recorded before the decisive results landed)

Re-running the screening parquets through the new `paired_sign_test` surfaced the
number that governs this whole comparison: on the articles where both configs
commit, `qwen2.5-coder:7b` and `qwen2.5:7b-instruct` **agree on the direction
95.6 % of the time** (7 discordant pairs out of 159). And every edge p-value in
the screening table is > 0.18 — *none* of those edges was distinguishable from
always calling the majority direction.

Two consequences, stated now so the decisive run cannot be read opportunistically:

1. **Directional skill may simply not be separable between these models.** If
   sign agreement stays ~95 % at n=1267, the discordant set is ~35 pairs and no
   sample available on this hardware resolves it. That is a real answer, not a
   failed measurement: it says model choice is close to irrelevant *for
   direction*.
2. **So the decision rule needs its tie-break made explicit.** If no config's
   `edge_r1` is significant at p < 0.05, fall through to the axes that *are*
   measured precisely, in order: `invalid_share` + `other_share` (does it fill
   the schema), `horizon_na_share` (does it answer the fields at all),
   `sent_coverage`, then `s/item`.

That fallback already points somewhere on the screening data, and it is the
sharper answer to "why are we using a code model for financial text". The
indictment is not that `qwen2.5-coder:7b` picks the wrong direction — that is
unmeasurable here. It is that the code model **fills the schema far worse**:
6.2 % invented/`other` event classes against 0.4 % for `qwen2.5:14b`, and it
declines to name a `horizon` on **41.9 %** of articles against 1.9–3.9 % for the
instruct models. Those gaps are ~15× and are not conditional on returns, so they
carry a tiny standard error — unlike the edge numbers, they are solid. The fields
the extraction exists to produce were the ones being lost.

## 12. What the scored corpus already proves (n=27,490, no GPU needed)

The screening samples are 259 articles, but the incumbent has **27,490 articles**
already scored under `event@2` on disk. Measuring the schema-fill metrics there
confirms the small-sample findings and settles what the bench cannot:

| metric | `event@1` (28,516) | `event@2` (27,490) |
|---|---|---|
| `other_share` | 33.3 % | **6.9 %** |
| `horizon` = n_a | 26.1 % | **39.4 %** |
| `materiality` ≤ 1 | 16.7 % | 39.2 % |
| distinct `sentiment` values | 23 | 45 |

Two things fall out. The v2 recalibration **worked** on `event_type` (33 % → 7 %
`other`) and on `materiality`. But it made `horizon` **worse** — 26 % → 39 % of
articles get no horizon at all, so the "`n_a` only when nothing is price-relevant"
instruction backfired. `horizon` is now the weakest field in the schema, and v3
only renames its escape hatch (`n_a` → `unclear`), which will not fix a 39 %
refusal rate. It needs redesign or removal, tracked as its own item.

### Anchoring is universal, not a model quirk

Sentiment values across all five screened models, same 259 articles:

| config | pos % | neg % | pos:neg | on a named anchor | `other` % | `horizon`=n_a % |
|---|---|---|---|---|---|---|
| qwen2.5-coder:7b | 51.2 | 27.5 | 1.86 | 98.8 % | 6.2 | **41.9** |
| qwen2.5:14b-instruct | 53.1 | 24.4 | 2.17 | 100 % | **0.4** | 3.9 |
| phi4:14b | 61.8 | 27.0 | 2.29 | 100 % | 0.8 | **1.2** |
| llama3.1:8b-instruct | 34.1 | 12.2 | 2.81 | 100 % | 4.7 | 5.5 |
| qwen2.5:7b-instruct | 60.2 | 18.9 | 3.18 | 100 % | 1.2 | 1.9 |

**98.8–100 % of every model's answers land exactly on the seven anchors the
prompt names.** That is as clean a confirmation as this data can give that naming
numbers determines the answers, and it is the strongest argument for v3's
labels-mapped-in-code design: the anchoring is a *schema* defect, so it is
fixable by the schema and cannot be fixed by swapping models.

### Two things the model does and does not decide

- **It decides schema fill, decisively.** `qwen2.5-coder:7b` emits 6–15× more
  junk `other` classes and refuses `horizon` 10–35× more often than the instruct
  models, confirmed at n=27,490. This is the real answer to "why are we using a
  code model for financial text" — not that its direction is wrong, but that it
  loses the fields the extraction exists to produce.
- **It does not decide direction, measurably.** 95.6 % sign agreement, no edge
  significant at p < 0.18.

### The positive skew caps the achievable edge

Every model skews positive, 1.86:1 to 3.18:1 — and the *incumbent code model is
the least skewed of the five*, so this is a property of the corpus and prompt, not
of the model. It also bounds what the sentiment field can deliver: a config that
says "positive" on 55–62 % of articles, against a market up on 51–54 % of
sessions, has very little room to beat the majority call. That is consistent with
every edge measured so far being indistinguishable from the trivial strategy.

The implication is a design one, not a model one: **absolute sentiment is close to
structurally incapable of directional edge here.** What can carry signal is a
*relative* framing — surprise against expectation rather than level — which is
exactly what `expectation_vs_outcome` (beat / in_line / miss) adds in v3, and why
that field, not the model swap, is the more promising lever.
