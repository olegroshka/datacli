# News Lane & Refresh — Roadmap

**Status:** items 1, 4 DONE · item 3 SUBSTRATE BUILT (90-day pass running) · 2, 5 PLANNED · **Created:** 2026-08-15  
**Context:** the `news` lane substrate is built and backfilled
(`EODHD_NEWS_SENTIMENT_FINDINGS.md`); this file is the ordered plan for turning it
into something models can consume, plus the refresh improvements surfaced by the
documentation review. One section per item: goal, scope, deliverables, acceptance,
size. Status is updated here as items land.

Design work for item 3 (own scoring) lives in `NEWS_SCORING_DESIGN.md` — it is the
one item that needs a design agreed before code.

---

## 1. Derived daily panel `news_symbol_daily`

**Goal.** A small, model-ready table so nobody has to scan 7 GB of text to ask
"how much / how positive was the news for X on day D".

**Scope.** Built locally from `news/articles/*.parquet` with DuckDB; no API calls.
Grain `(date, ticker, exchange)` for every symbol tag in the corpus (not only our
universe — it is cheap and the join happens later). Columns (v1):

| column | meaning |
|---|---|
| `n_articles` | distinct `article_id`s tagging the symbol that UTC day |
| `share_of_day` | `n_articles / global articles that day` — the volume feature to use (2024 dip, tagging bursts) |
| `n_sources` | distinct `source` hosts |
| `polarity_mean`, `polarity_std`, `pos_share` | vendor sentiment aggregates (`polarity > 0.05`) |
| `n_solo` | articles where the symbol is one of ≤ 3 tags (proxy for "about this company") |
| `first_published_at`, `last_published_at` | intraday bounds, for event-study alignment |

**Deliverables.** `eodhd/build_news_symbol_daily.py` (incremental: rebuilds only
days whose article partition is newer than the panel's), a `news_symbol_daily`
dataset in the `news` lane (kind `news_daily`, ticker-keyed, so `describe`/`find`/
`coverage`/`reindex` and `status` cover it), a DuckDB view, a `refresh` post-step
after the news top-up, tests, doc section in the findings file, lab schema context.

**Acceptance.** `describe AAPL.US` shows the panel; `status news` shows both
datasets; rebuild of the full history < 10 min; `refresh --run` keeps it current
without extra flags.

**Status.** DONE 2026-08-15 — `eodhd/build_news_symbol_daily.py`; kind `news_daily`
in the news lane (ticker-keyed, snapshot); DuckDB view `news_daily`; runs after the
news top-up in `refresh` (default kind); full build 6.95 M rows / 104 MB in ~2 min,
incremental rebuilds only new/changed days. Columns as specified plus `symbol`,
`n_articles_day`, `neg_share`, `built_at`.

**Size.** ~1 day.

## 2. Issuer mapping (UK/EU coverage)

**Goal.** An EU/UK issuer sees the articles tagged with its US line, ADR and
Frankfurt/Munich lines (`SAP.XETRA` gets the 248 `SAP.US` articles, not 1).

**Scope.** `issuer_map.parquet`: `(ticker, exchange) → issuer_id` built from
`firm_metadata.parquet` (ISIN, name, CIK where present) across `us_common` and
`uk_eu`, plus the symbol-tag co-occurrence in the corpus itself (symbols that
co-occur in > X % of each other's articles with the same name stem). Panel from
item 1 gains an `issuer`-grain variant.

**Deliverables.** `eodhd/build_issuer_map.py`, `news_issuer_daily` dataset,
tests, a QC of the mapping (collisions, coverage per lane).

**Acceptance.** Every `uk_eu` ticker with fundamentals resolves to an issuer;
spot-check list (SAP, HSBA, ASML, SIE, NESN, MC, VOD) maps to the expected lines.

**Size.** ~2 days (the matching rules need iteration).

## 3. Own scoring over the text (pluggable, dynamic categories)

**Goal.** Replace the vendor's coarse VADER-style score with our own,
model-agnostic scoring: pluggable backends (local model, API model, classical
classifier, embeddings), categories defined declaratively and changeable
without touching code, outputs stored as `article_id`-keyed sidecars that never
mutate the raw corpus.

**Status.** SUBSTRATE BUILT — `llm/` + `scoring/` (see `NEWS_SCORING_DESIGN.md` §8–9);
next: the 90-day local pass, `score eval`, gold set, embeddings, refresh top-up.

**Size.** Design ½ day; substrate 2–3 days; first full scoring pass depends on the
backend/tier chosen (see cost model in the design doc).

## 4. Refresh improvements

Surfaced by the documentation review; small, independent, worth doing together.

- **`fetch_eodhd_news.py --dry-run`** — print the pending-day plan and the unit
  estimate without crawling (the only fetch path without a dry-run today).
- **State drift on the price lanes.** The bulk `--fast` dry-run currently plans
  185 calls ≈ 18.5k units because the lanes are ~7 days behind; a routine cadence
  (daily `refresh --fast --run`) keeps that at ~4k. Document the cadence in the
  runbook and add a `status` warning when the newest sidecar date is > `--days`
  behind (the bulk path skips such pairs, so they silently stop advancing).
- **QC lane map from the registry.** `report_eodhd_raw_quality.py` keeps its own
  hardcoded `LANES`; derive it from `eodhd_datasets.LANES` (universe path,
  default exchange, include-events from the dataset kinds) so a new lane is
  audited automatically — the last consumer that is not registry-driven.
- **`refresh` post-steps.** Let a dataset spec declare a local post-step (item 1's
  panel build) so `refresh --run` produces derived tables without extra flags.
- **Manifests.** Refresh the dated `EODHD_*_MANIFEST.md` "current observed" counts
  from `status --write` output rather than by hand (a `--manifests` flag).

**Status.** DONE 2026-08-15 — `fetch_eodhd_news.py --dry-run` (pending days + unit
estimate, no key needed); `status` computes `pairs_behind` per state-backed
dataset and prints a yellow catch-up line (also a "Catch-up needed" section in
STATUS.md) — on the day it landed it showed all 2,595 `us_common` price pairs 8 days
behind, i.e. a plain `--fast` would have skipped them all; QC's lane map is derived
from the registry (`lanes_from_registry`, universe-file metadata now on
`LaneConfig`); `DatasetSpec.local` marks no-API steps and the refresh plan says how
many steps hit the API; the post-step need is covered by `news_daily` being a
default kind; the manifests now carry a "Live counts" pointer to `status` /
`STATUS.md` instead of a generator.

**Size.** ~1 day total.

## 5. Gap-fill and hygiene

- **Per-ticker gap-fill 2019–2020** for the universe: the global feed is sparse
  before 2021 (~700/day) but `/news?s=` per symbol may return more for large
  caps; measure on 20 tickers before deciding (5 units/page).
- **Lab personas / skills:** add a `news` skill (coverage + polarity sanity per
  lane) and let the `event-study` persona use `news_symbol_daily`.
- **Corpus hygiene report:** empty-content share, junk symbol tags
  (`USDUSD.FOREX`), re-publication rate, per-source volume — as a `qc news`
  extension once the QC map is registry-driven (item 4).

**Size.** ~1 day.

---

## Order and dependencies

1 → 2 (panel first, issuer variant on top) · 3 independent of 1–2 but consumes
their tables for evaluation · 4 anytime (the post-step piece before 1 lands is
nicer) · 5 last.
