# News Lane & Refresh — Roadmap

**Status:** items 1, 2, 4, 5 DONE · item 3 SUBSTRATE BUILT (90-day pass running) · **Created:** 2026-08-15  
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

**Status.** DONE 2026-08-15 — `build_issuer_map.py` (vendor `LEI`/`ISIN`/
`PrimaryTicker`/`Listings` from the fundamentals cache + corpus co-tagging with
`P(parent|line) ≥ 0.9`, `n ≥ 30`, ETF/index/junk exclusions and a *different-known-
identity* guard against peer contamination; 22.6k symbols → 12.5k issuers, 0 LEI
collisions) and `build_news_issuer_daily.py` (issuer-grain panel, each article
counted once per issuer, rows for every covered member ticker; 2.74 M rows). Both
are local default kinds after `news_daily`; views `issuer_map` /
`news_issuer_daily`; `describe`/`rows` cover the panel. 30-day check: SAP.XETRA
1 → 243 articles, HSBA.LSE 6 → 308, ASML.AS 127 → 506, VOD.LSE 2 → 36. Known
misses: ADR lines with `P` just under 0.9 (Nestlé's NSRGY.US at 0.80) stay
separate — a `--min-p` knob exists; the vendor `Listings` block fills most such
gaps over time.

**Size.** ~2 days (the matching rules need iteration).

## 3. Own scoring over the text (pluggable, dynamic categories)

**Goal.** Replace the vendor's coarse VADER-style score with our own,
model-agnostic scoring: pluggable backends (local model, API model, classical
classifier, embeddings), categories defined declaratively and changeable
without touching code, outputs stored as `article_id`-keyed sidecars that never
mutate the raw corpus.

**Status.** SUBSTRATE BUILT and the objective has **changed on evidence** — see
`NEWS_SCORING_DESIGN.md` §8–14. The short version:

- **Direction does not predict.** Confirmed three independent ways (803 committed
  article rows, p = 0.78; 2,564 days of the vendor signal, t = 1.27; 11 of our
  days, t = 1.54). Every significant direction number is *contemporaneous*, and
  five models agreed on direction 95.6 % of the time, so no model choice fixes it.
- **Magnitude does predict, and it needs the model.** `materiality` orders the next
  session's absolute market-adjusted move monotonically across all four levels
  (155 → 286 bps, spread +130 bps, t = 6.14), while counting a name's articles —
  free, no model — gets +21 bps and t = −0.29 on the same days. The free baseline
  wins the *same* session (t = 15.25 over 2,215 days) and nothing forward.
- **The vendor's sentiment field is unusable**: ≥ 0.99 on 49.9 % of rows, only
  4.6 % negative. It cannot order a cross-section at all.

So the deliverable is an **event-driven magnitude/volatility feature**, not a
directional signal. `event_v4` spends its complexity there (`expected_move` in
buckets of realised move, `materiality` kept alongside for a paired comparison,
`horizon` with its escape hatch removed, `sentiment` demoted to 5 descriptive
levels). Model settled on `qwen2.5:14b-instruct` — chosen on schema fill, where
the differences are large and precisely measured (0.4 % junk classes vs the
inherited code model's 6.9 %; 3.9 % vs 39.4 % `horizon` refusals), not on edge
numbers that cannot separate models.

**Next.** The v4 pass over 13,200 articles × 88 days, to (a) test `expected_move`
against `materiality` paired in the same call and (b) extend the t = 6.14 result
from 15 days to 88 — the finding the whole redirection rests on. Then embeddings,
a gold set, and the refresh top-up.

**Size.** Design ½ day; substrate 2–3 days; the v4 pass ~15 h of local GPU.

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
- **Atomic parquet writes:** the price/event fetchers and the bulk path rewrite
  `prices_daily.parquet` in place, so a reader during a refresh can see a torn
  file (seen once during the `--fast --days 10` catch-up) and a crash mid-write
  loses the dataset. Write to `<name>.parquet.tmp` and `os.replace`
  (`build_news_issuer_daily.write_panel` already does).
- **Corpus hygiene report:** empty-content share, junk symbol tags
  (`USDUSD.FOREX`), re-publication rate, per-source volume — as a `qc news`
  extension once the QC map is registry-driven (item 4).

**Status.** DONE 2026-08-16 —
- Gap-fill: the probe showed the corpus had **no** 2019–2020 at all (the backfill
  started at 2021-01-01) while the vendor holds it (AAPL 344, PFE 251, MSFT 206
  articles per symbol in those two years). Per-ticker pulls were unnecessary: the
  **global crawl** was extended (`fetch_eodhd_news.py --from 2019-01-01 --to
  2020-12-31`): 731 days, 841 pages ≈ 4.2k units, **441,330 articles**, 0 failures;
  panels rebuilt incrementally. The corpus now spans 2019-01-01 → today.
- `qc news` → `report_news_quality.py`: crawl gaps / state, empty & untagged
  shares, junk symbols, re-publications, tagging bursts, volume by year, sources
  (trailing 365 days by default, `--all` for history).
- Lab skill `news-coverage`; personas already point at the news views.
- Atomic parquet/CSV writes everywhere (`eodhd/_atomic.py`).

**Size.** ~1 day.

---

## 6. Price data hygiene (found 2026-08-17, blocks return-based work)

Surfaced while building the market adjustment for `score bench`: the price store
carries daily returns of **+24,500 %** (US), **+29,900 %** (INDX) and
**+19,294 %** (LSE), with minima near −99.6 %. These are almost certainly
unadjusted splits, delisted stubs and bad ticks rather than real moves.

Impact is not confined to the bench. Any equal-weight aggregate over `prices` is
unusable — the per-exchange daily mean return has sd 0.9–3.9 % and worst-day
values of 3–16 %, against 0.19–0.40 % for the median. `score bench` works around
it by using the median as its market proxy, but that is a workaround, not a fix,
and every future return-based metric will hit the same wall.

- [ ] `qc prices`: flag `|return| > 50 %` bars, per-exchange counts and worst
      offenders, in the shape of the existing `qc news`
- [ ] decide the policy — drop, winsorise, or re-fetch the affected symbols — and
      apply it where returns are computed rather than at each call site
- [ ] check whether `adjusted_close` is actually split-adjusted for the offenders,
      since that is the most likely root cause

## Order and dependencies

1 → 2 (panel first, issuer variant on top) · 3 independent of 1–2 but consumes
their tables for evaluation · 4 anytime (the post-step piece before 1 lands is
nicer) · 5 last · 6 before any further return-based evaluation is trusted.
