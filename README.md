# datacli

**An interactive data-operations shell for market data** — download, refresh,
quality-check and explore raw provider snapshots from one cohesive, colour-coded
terminal UI.

datacli turns a directory of raw parquet/CSV snapshots into a queryable,
auditable dataset. It ships a registry-driven acquisition toolkit for
[EODHD](https://eodhd.com) (prices, dividends, splits, fundamentals across US and
UK/EU equities, ETFs and indices, plus a global full-text **news / sentiment
corpus**), a macro source (FRED + EODHD series), a DuckDB-backed explorer for
fast ad-hoc questions, an optional LLM-backed "Raw Data Lab", an MCP server, and
a one-way cloud backup — all behind a single REPL with tab-completion.

```text
                                     EODHD status · as-of 2026-07-11 · stale >7d

 lane        dataset            last_data    coverage     fetched         rows   pairs   age   flag      state
 ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
 us_common   ● prices           2026-07-08   2026-07-08   07-09 11:06   12.66M   2,595    3d   ✓ ok      ok 2,593 · empty 2
             ● dividends        2026-07-09   2026-07-09   07-09 07:50   168.7K   2,595    2d   ✓ ok      ok 1,895 · empty 700
             ● splits           2026-07-07   2026-07-08   07-08 19:33     5.9K   2,581    3d   ✓ ok      ok 1,879 · empty 702
             ● fundamentals_q   2026-07-02   -            07-08 19:33    1.21M   6,124    9d   ⚠ STALE   ok 5,895 · empty 229

3 fresh · 1 stale · 0 absent · ≈14.05M rows across 4 datasets
```

---

## Highlights

- **One shell, every operation** — a cmd2 + Rich REPL with source contexts,
  tab-completion, and slash-optional commands (`qc` == `/qc`).
- **Registry-driven acquisition** — lanes and datasets live in a single registry;
  add one entry and `status`, `refresh`, `lanes` and the explorer pick it up.
- **Freshness at a glance** — a colour-coded *as-of* dashboard that measures
  staleness against the correct anchor per dataset kind (last bar for prices,
  query coverage for events, latest filing for fundamentals, last crawled day for
  news).
- **Actionable QC** — a triage view that flags structural issues and tells you the
  fix (`targeted_rerun` vs. `full_refresh`), scoped by lane and dataset.
- **Ask questions, not SQL** — `describe` / `find` / `rows` / `coverage`, plus a raw
  `sql` escape hatch, all over a warm DuckDB connection.
- **Schema-versioned & evolution-safe** — projected views NULL-fill or rename-alias
  columns so queries stay stable as a provider's schema drifts.
- **Fast refresh** — bulk end-of-day endpoints turn an hours-long per-ticker top-up
  into minutes.
- **A news corpus you own** — 4.5 M full-text articles (2021→) with symbol tags and
  vendor sentiment, stored as daily parquet partitions and queryable in DuckDB.
- **Cohesive by design** — every command shares one console and palette: dataset
  hues, severity glyphs, freshness colours, and degrades cleanly under `NO_COLOR`
  or a pipe.

## Contents

- [Requirements & cost](#requirements--cost) · [Install](#install) ·
  [Lifecycle at a glance](#lifecycle-at-a-glance)
- [Quickstart A — you already have snapshots](#quickstart-a-you-already-have-snapshots) ·
  [Quickstart B — from zero, with only an API key](#quickstart-b-from-zero-with-only-an-api-key)
- [The shell](#the-shell) · [Command reference](#command-reference)
- [Data acquisition](#data-acquisition-status--refresh--qc) ·
  [The news lane](#the-news-lane) ·
  [Exploring the data](#exploring-the-data)
- [Schema & versioning](#schema--versioning) ·
  [Raw Data Lab](#raw-data-lab-optional-llm-backed) · [MCP](#use-your-data-from-claude-code--cursor-mcp) ·
  [Backup](#backup-sync) ·
  [Configuration](#configuration--where-data-lives)
- [Architecture](#architecture) · [Development](#development) ·
  [Provenance](#provenance)

## Requirements & cost

- **Python ≥ 3.11** and [**uv**](https://docs.astral.sh/uv/) for environment and
  dependency management. Examples are PowerShell; the commands are the same
  elsewhere.
- **An EODHD API key** — only for fetching; exploring existing snapshots needs no key.
  The endpoints used (`/fundamentals`, `/calendar/earnings`, `/eod`, `/div`,
  `/splits`, `/eod-bulk-last-day`, `/news`, `/macro-indicator`) sit in EODHD's
  all-inclusive tier; the account this was built against reports a
  **100,000 API-unit daily limit**.
- **What costs units:** `refresh --run` (fetch), `probe`, `macro fetch --run`. Everything
  else — `status`, `qc`, `lanes`, `describe`/`find`/`rows`/`sql`, `schema`,
  `reindex`, `sync`, and every dry-run — runs offline against your local snapshot.
  Rough scale: a routine `refresh --fast --run` costs ~100 units per exchange per
  day behind per kind (≈ 4k units/day for the 13 exchanges × 3 kinds; the dry-run
  prints the exact planned count); a **first fill of every lane is a multi-hour,
  roughly two-quota-day job** (fundamentals ≈ 10 units/firm × ~14k firms ≈ 140k,
  plus prices/events, plus ≈ 28k for the news backfill).
- Optional extras: a **model** for the Raw Data Lab (local Ollama is free), a
  **`FRED_API_KEY`** for the FRED half of `macro`, a **Google OAuth client** for
  Drive backup.

## Install

```powershell
uv sync --extra dev
```

## Lifecycle at a glance

Every command belongs to one stage; `uv run python eodhd/cli.py --help` prints a
compact version of this map. `$` = hits the paid EODHD API (needs `EODHD_API_KEY`).

| stage | what you run | notes |
|---|---|---|
| **1. setup** | `config set data-root <path>` · set `EODHD_API_KEY` · `lanes` | data root = where the parquet lives; the key is an env var, never in config |
| **2. first fill** `$` | `refresh <lane> --datasets fundamentals --run` → `refresh --run` → `python eodhd/fetch_eodhd_news.py` | order matters for the common-stock lanes; see [Quickstart B](#quickstart-b-from-zero-with-only-an-api-key) |
| **3. routine** `$` | `refresh --fast --run` (daily; prices/events via bulk + news top-up + local panel build) · `refresh --datasets fundamentals --run` (weekly) | `--fast` fills at most `--days` (7) back; `status` warns when pairs have fallen further behind |
| **4. verify** | `status [lane]` · `qc [lane] [dataset]` | freshness + structural QC with the remediation per finding |
| **5. index** | `reindex` | `describe`/`find` read the catalog — **run it after every fetch** |
| **6. explore** | `describe` · `find` · `rows` · `coverage` · `sql` | `sql` also covers `news`, `news_state`, `catalog`, macro views |
| **7. score** | `score plan` → `score run --run` | own scores over the news corpus, local model, resumable; see [`eodhd/NEWS_SCORING_DESIGN.md`](eodhd/NEWS_SCORING_DESIGN.md) |
| **8. ask** | `ask` · `agent` · `investigate` · `lab run` | optional LLM lab; grounded, read-only |
| **9. back up** | `sync` (plan) → `sync push --run` | Google Drive or a local directory |

## Quickstart A — you already have snapshots

Three steps take you from clone to an answered question:

```powershell
uv sync --extra dev

# 1. tell datacli where your raw data lives (writes datacli.toml, git-ignored)
uv run python eodhd/cli.py config set data-root "D:\data\raw\eodhd"

# 2. build the query index (a small catalog parquet next to your data)
uv run python eodhd/cli.py reindex

# 3. ask a question
uv run python eodhd/cli.py describe VAR.OL
```

```text
VAR.OL  ->  lane(s): uk_eu
                                    VAR.OL
┌──────────────┬─────────┬──────┬────────────┬────────────┬──────────┬───────┐
│ dataset      │ present │ rows │ first      │ last(data) │ coverage │ state │
├──────────────┼─────────┼──────┼────────────┼────────────┼──────────┼───────┤
│ prices       │ · no    │    - │ -          │ -          │ -        │ -     │
│ dividends    │ · no    │    - │ -          │ -          │ -        │ -     │
│ splits       │ · no    │    - │ -          │ -          │ -        │ -     │
│ fundamentals │ ✓ yes   │   68 │ 2020-12-31 │ 2025-09-30 │ -        │ ok    │
└──────────────┴─────────┴──────┴────────────┴────────────┴──────────┴───────┘
```

Resolution precedence for the data root:

```text
EODHD_DATA_ROOT env var   >   datacli.toml [eodhd].data_root   >   ../btest/data/raw/eodhd (legacy default)
```

## Quickstart B — from zero, with only an API key

An empty data root needs a *first fill*, and its order is not arbitrary: the
common-stock lanes' price/event fetchers read `coverage_summary.csv`, which only
the **fundamentals** stage writes. `refresh` knows this — it reorders or skips and
prints a `note:` telling you what to run — but you should still budget for it:
hours of wall-clock and roughly two quota-days for everything.

```powershell
uv sync --extra dev
$env:EODHD_API_KEY = "<your key>"           # persist: setx EODHD_API_KEY <your key>
uv run python eodhd/cli.py config set data-root "D:\data\raw\eodhd"
uv run python eodhd/cli.py config           # data root exists? key resolves?

# 1. common-stock lanes: fundamentals first (writes the coverage file), then prices/events
uv run python eodhd/cli.py refresh us_common uk_eu --datasets fundamentals --run
uv run python eodhd/cli.py refresh us_common uk_eu --run

# 2. ETF / index lanes bootstrap themselves (universe step first)
uv run python eodhd/cli.py refresh us_etf index_ref uk_eu_etf uk_eu_index_ref --run

# 3. news backfill (2021 -> today, ~3 h, ~7 GB, ~28k units) -- deliberately NOT part of refresh
uv run python eodhd/fetch_eodhd_news.py --dry-run     # pending days + unit estimate, crawls nothing
uv run python eodhd/fetch_eodhd_news.py

# 4. verify, index, explore
uv run python eodhd/cli.py status
uv run python eodhd/cli.py reindex
uv run python eodhd/cli.py describe AAPL.US
```

Every fetcher is resumable: if a run is interrupted, re-run the same command
without `--full-refresh`. From then on the [routine](#lifecycle-at-a-glance) is
`refresh --fast --run` daily and `refresh --datasets fundamentals --run` weekly.
Full detail — per-lane run order, windowed reruns, resume semantics — is in
[`eodhd/README.md`](eodhd/README.md).

## The shell

```powershell
uv run python datacli.py
```

```text
data>  sources                  # list data sources
data>  config set data-root D:\data\raw\eodhd   # config works at any prompt
data>  source eodhd             # enter a source context -> eodhd>
eodhd> status                   # colour-coded as-of dashboard (all lanes)
eodhd> status us_common         # ... scoped to one lane
eodhd> fetch --fast --run       # bulk top-up (minutes, not hours) + news top-up
eodhd> qc us_common splits      # quality triage, drilled into one dataset
eodhd> reindex                  # refresh the catalog after fetching
eodhd> describe VAR.OL          # everything about one ticker
eodhd> source macro             # switch to the macro source -> macro>
macro> status                   # FRED + EODHD macro series and their coverage
```

`status` reports the **current** source's datasets only — each source owns its own
data — so it prints a pointer to the peer sources underneath. `macro` is a
first-class source alongside `eodhd`; `macro status` is just a shortcut that saves
you from `source macro` first. The shell's `fetch` is the eodhd CLI's `refresh`
(both names work in the shell).

`sources` lists the available adapters and the commands each exposes:

```text
                                           data sources
┌─────────┬───────────────────────────────────────────┬───────────────────────────────────────────┐
│ source  │ summary                                   │ commands                                  │
├─────────┼───────────────────────────────────────────┼───────────────────────────────────────────┤
│ eodhd   │ US/UK-EU equities, ETFs, indices,         │ status fetch refresh qc lanes probe       │
│         │ fundamentals + global news corpus         │ describe find rows coverage sql config    │
│         │                                           │ schema reindex                            │
│ macro   │ FRED + EODHD macro series (rates,          │ status list fetch                         │
│         │ country indicators, index/FX)             │                                           │
│ fred    │ FRED economic series (adapter; plugin     │ load-only (no ops tooling yet)            │
│         │ deferred)                                 │                                           │
│ yahoo   │ Yahoo Finance                             │ load-only (no ops tooling yet)            │
│ csv     │ Local CSV files                           │ load-only (no ops tooling yet)            │
│ parquet │ Local parquet files                       │ load-only (no ops tooling yet)            │
└─────────┴───────────────────────────────────────────┴───────────────────────────────────────────┘
```

Shell conveniences:

- **Slash-optional** — the leading `/` is optional; `qc` and `/qc` are identical.
- **Tab-completion** over the known sets — `qc us_<Tab>` completes lanes,
  `qc us_common <Tab>` completes datasets, and `status` / `rows` / `config` /
  `fetch` / `source` complete their arguments too.
- **`help`** lists commands grouped by stage (global → acquire/verify → explore → lab), **`clear`** wipes the screen,
  **`quit`** (or **`exit`**) leaves.

## Command reference

Run any command with `--help` for its full options.

**Offline — works on the local snapshot, no key, no model:**

| Command | What it does | Direct entry point |
|---|---|---|
| `status [lane]` | As-of dashboard: what data exists and how fresh it is | `eodhd/cli.py status` |
| `qc [lane] [dataset]` | Raw-data quality triage with recommended fixes (price-bearing lanes); `qc news` = corpus hygiene (gaps, empty/untagged, junk tags, bursts) | `eodhd/cli.py qc` |
| `lanes` | List registered lanes, datasets, universe sources and fetchers | `eodhd/cli.py lanes` |
| `describe TICKER` | Everything about one ticker, across datasets (reads the catalog → `reindex` first) | `eodhd/cli.py describe` |
| `find PATTERN` | Locate a ticker (lane / exchange / datasets) (reads the catalog) | `eodhd/cli.py find` |
| `rows TICKER DATASET` | Show the actual rows for a ticker in a dataset (ticker-keyed datasets) | `eodhd/cli.py rows` |
| `coverage TICKER` | Do the datasets cover a ticker equally? | `eodhd/cli.py coverage` |
| `sql "<query>"` | Raw DuckDB over every view incl. `news` (unguarded in the CLI; the lab/MCP paths are read-only) | `eodhd/cli.py sql` |
| `schema` | Declared schema version + drift vs. on-disk data | `eodhd/cli.py schema` |
| `reindex` | (Re)build the fast query catalog after new data | `eodhd/cli.py reindex` |
| `config [set <key> <value>]` | Show / edit configuration (`data-root`, `sync-*`) | `eodhd/cli.py config` |
| `sync [status \| push --run \| login]` | One-way backup of the data root (Google Drive or local dir; dry-run unless `--run`) | `python -m storage.cli` |
| `macro status \| list` | The macro source's coverage / catalog | `python -m macro.cli` |
| `score plan \| run --run \| status` | Schema-driven scores over the news corpus with a **local** model by default (`event_v1`: event type, summary, sentiment, per-symbol direction); paid models only with `--budget-usd` | `python -m scoring.cli` |

**Hits a provider — spends EODHD units (`$`) or needs a provider key:**

| Command | What it does | Direct entry point |
|---|---|---|
| `fetch` / `refresh [lanes] [--fast] [--run]` `$` | Download / top up data (dry-run unless `--run`) | `eodhd/cli.py refresh` |
| `probe TICKER…` `$` | Ad-hoc availability probe; caches raw payloads under `<data-root>/probe_cache/`, never touches lane outputs | `eodhd/cli.py probe` |
| `macro fetch [--run]` | Pull FRED (needs `FRED_API_KEY`) + EODHD macro series (`$`) | `python -m macro.cli fetch` |

**Agentic — the [Raw Data Lab](#raw-data-lab-optional-llm-backed)** ✦ *(needs a model — see note below):*

| Command | What it does |
|---|---|
| `ask "<question>"` ✦ | Grounded natural-language Q&A from the default persona |
| `agent <persona> "<q>"` ✦ | Ask a named persona (`macro-strategist`, `microstructure`, …) |
| `investigate "<topic>" [--generator <persona>] [--no-report]` ✦ | Multi-agent generator → skeptic → reporter; writes a verified report |
| `lab run <skill> [args] [--verify] [--no-report]` ✦ | Run a saved EDA playbook → reproducible report |
| `lab agents` · `lab skills` · `lab config` | Roster · playbooks · models/budget/keys (`config` needs no model) |

Direct entry points are `uv run python <entry point> …`; the agentic commands are
`uv run python -m lab.cli <command>` (and work unprefixed inside the shell).

> ✦ **Needs a model.** Install the lab (`uv sync --extra lab`) and have at least one
> model available: a **local Ollama** model (free — `ollama pull qwen2.5-coder:7b`)
> and/or an `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in your environment. Serious
> personas default to **Opus**; the everyday `analyst` and mechanical `auditor` /
> `quant` run **free on local**. Run **`lab config`** to see exactly what's configured
> and available.

## Data acquisition: status · refresh · qc

`status` measures staleness against the correct freshness anchor for each dataset
kind — the last *bar* for prices, the query *coverage* ceiling for events (so a
legitimately future-dated dividend is never mistaken for an anomaly), the latest
filing for fundamentals, the last crawled UTC day for news. See the dashboard at
the [top of this page](#datacli). On an empty root it says so and points at the
first-fill steps.

`refresh` prints an ordered plan and does nothing until you add `--run` (it hits a
paid API). Per lane the order is universe → prices → dividends → splits →
fundamentals → news. If a common-stock lane's coverage file is missing, it
reorders fundamentals first (or skips the per-ticker steps and prints the fix).
`--fast` uses bulk end-of-day endpoints — one call per exchange instead of one per
ticker, turning hours into minutes — for lanes that already have state; the news
top-up still runs after it. `--fast` is a top-up, not a first fill.

`qc` audits the raw data and ranks the findings, telling you the remediation for
each. Scope it to a lane, or drill into a single dataset for the uncapped list:

```text
QC · us_common   ✗ 26 errors · ⚠ 113 warnings ─────────────────────────────────────────────────────────────────

 dataset     universe   out_pairs   state     rows   range                     err   warn
 ────────────────────────────────────────────────────────────────────────────────────────
 prices         2,595       2,595   2,595   12.66M   1990-01-02 → 2026-07-08     7     94
 dividends          -       1,895   2,595   168.7K   1970-01-19 → 2026-07-09     0      0
 splits             -       1,884   2,581     5.9K   1962-10-31 → 2026-07-07    19     19

  top issues  stale_latest_price 70 · sparse_recent_history 18 · missing_state 14 · missing_audit_row 14

     dataset   ticker     issue                      action           detail
 ─────────────────────────────────────────────────────────────────────────────────────────
 ✗   prices    BK.US      bad_state_status           targeted_rerun   Unexpected state status: empty
 ✗   prices    NIXXW.US   invalid_ohlc_relationsh…   full_refresh     Rows with invalid high/low relationships: 1
 ✗   prices    MAYS.US    non_positive_prices        full_refresh     Rows with non-positive prices: 9
 ✗   splits    BK.US      empty_with_rows            full_refresh     State says empty but event parquet has 6 rows
   … 131 more  →  qc us_common prices
```

`lanes` shows the registry that drives all of the above — each dataset with a
kind-hued dot, where its universe comes from, and the fetcher that populates it:

```text
                                            EODHD lanes (7)

 lane            region / class    dataset          fetcher
 ─────────────────────────────────────────────────────────────────────────────────────────────────────
 us_common       US / common       universe         (from the fundamentals stage: coverage_summary.csv)
                                   ● prices         fetch_eodhd_us_prices.py
                                   ● dividends      fetch_eodhd_us_dividends.py
                                   ● splits         fetch_eodhd_us_splits.py
                                   ● fundamentals…  fetch_eodhd_us_fundamentals.py --update

 uk_eu           UK/EU / common    universe         (from the fundamentals stage: coverage_summary.csv)
                                   ● prices         fetch_eodhd_eu_prices.py
                                   …
 news            Global / news     universe         (no universe)
                                   ● news_articles  fetch_eodhd_news.py --limit-days 30
```

See [`eodhd/README.md`](eodhd/README.md) for the full acquisition runbook
(first fill, resume model, bulk refresh, fundamentals, per-lane manifests).

## The news lane

`news` is a global, article-level corpus crawled once per UTC day from EODHD's
`/news` feed: `title`, full `content`, `link`, `source`, vendor `symbols` and
`tags` (list columns), and the vendor's per-article sentiment
(`polarity/neg/neu/pos`). Measured on this account: ≈ 2.2k articles/day on average
(≈ 1.2k in the thin 2024, ≈ 2.5k recently), dense from 2021, ≈ 3.4 MB/day on disk;
the full backfill is 4.46 M articles / 6.96 GB / 5,511 pages ≈ 28k units and took
~3 h single-threaded.

- **Backfill** is an explicit `uv run python eodhd/fetch_eodhd_news.py` (uncapped);
  the routine `refresh` only tops up the newest days (capped, so it can never turn
  into a backfill by accident).
- **Query it with `sql`** — `news` (article-level) and `news_state` (one row per
  crawled day). The ticker verbs (`describe`/`find`/`rows`/`coverage`) and `qc`
  skip it because it is day-keyed, not ticker-keyed.
- **Ask it per ticker and day** — `news_daily` (`news_symbol_daily.parquet`, built
  locally by `refresh` after the news top-up): one row per `(date, ticker, exchange)`
  with `n_articles`, `share_of_day` (volume normalised by that day's global count),
  `n_solo`, `n_sources`, vendor `polarity_mean/pos_share/neg_share`. `describe`,
  `rows`, `coverage` and the catalog cover it like any ticker-keyed dataset.
- **…and per issuer** — `news_issuer_daily` maps every tag line to its issuer
  (`issuer_map`: vendor LEI/ISIN/listings + corpus co-tagging) and counts each
  article once per company, so a UK/EU ticker sees its US line/ADR/Frankfurt
  mirrors' coverage (SAP.XETRA 1 → 243 articles/month). Rows exist for every
  covered ticker of the issuer.
- **Score it yourself** — `score plan` / `score run --run` extract a rich event
  record per article (`event_v1`: event type, summary, sentiment, materiality,
  horizon, per-symbol role/direction) with a **local** model by default and write
  `article_id`-keyed sidecars; query `news_scores_event`. Design and cost model:
  [`eodhd/NEWS_SCORING_DESIGN.md`](eodhd/NEWS_SCORING_DESIGN.md).
- **Know its quirks** before modelling on it: the vendor sentiment is a coarse
  VADER-style score, symbol tagging is US-biased (an EU issuer's US line collects
  most of the tags), ~14 % of articles carry no symbol at all, and daily volume is
  not stationary across years. All of it is documented, with numbers, in
  [`eodhd/EODHD_NEWS_SENTIMENT_FINDINGS.md`](eodhd/EODHD_NEWS_SENTIMENT_FINDINGS.md).

```text
eodhd> sql "SELECT s AS symbol, count(*) n FROM news, unnest(symbols) t(s)
            WHERE date >= '2026-08-01' GROUP BY 1 ORDER BY 2 DESC LIMIT 5"
```

## Exploring the data

**Three ways to ask, in increasing flexibility:** entity verbs
(`describe` / `find` / `rows` / `coverage`) for instant structured answers with no
SQL; a raw **`sql`** escape hatch when you want full control; and the
natural-language **[Raw Data Lab](#raw-data-lab-optional-llm-backed)** when you'd
rather state the question than write the query. Everything runs over the raw parquet
via a warm DuckDB connection, and each verb also works as a direct
`eodhd/cli.py <verb>`.

`describe` and `find` read the **catalog** built by `reindex`; after a fetch, run
`reindex` or they will report the pre-fetch counts. `rows` and `sql` always read the
parquet directly.

`find` fuzzy-searches tickers across every dataset:

```text
              matches for 'VAR' (6)
┌────────┬──────────┬────────┬──────────────────┐
│ ticker │ exchange │ lane   │ datasets         │
├────────┼──────────┼────────┼──────────────────┤
│ AVARDA │ ST       │ uk_eu  │ fundamentals     │
│ CVAR   │ US       │ us_etf │ dividends prices │
│ VAR    │ OL       │ uk_eu  │ fundamentals     │
│ VARN   │ SW       │ uk_eu  │ fundamentals     │
└────────┴──────────┴────────┴──────────────────┘
```

`rows` shows the latest rows for a ticker in a dataset (narrow columns by default,
`--cols *` for everything):

```text
       VAR.OL in fundamentals (latest 20)

 statement   date         filing_date   currency
 ───────────────────────────────────────────────
 IS          2025-09-30   2025-09-30    USD
 BS          2025-09-30   2025-09-30    -
 CF          2025-09-30   2025-09-30    USD
 CF          2025-06-30   2025-06-30    USD
```

`sql` is the raw escape hatch, running DuckDB against views named `prices`,
`dividends`, `splits`, `fundamentals`, `news` (plus their `*_state` sidecars, the
`catalog` once reindexed, and `macro` / `macro_country` / `macro_market` once
fetched). Every EODHD view carries a `lane` column:

```text
eodhd> sql "SELECT lane, count(*) AS n, min(ex_date) AS earliest
            FROM dividends GROUP BY lane ORDER BY n DESC"

 lane              n   earliest
 ────────────────────────────────
 us_common   168,714   1970-01-19
 us_etf      158,344   1986-10-08
 uk_eu_etf    66,807   2000-08-29
 uk_eu        49,079   1972-07-29
```

## Schema & versioning

The columns the tool relies on are declared in `eodhd/schema.py` with a
`SCHEMA_VERSION` (currently 2: v1 datasets + `news`). Queries stay stable as data
evolves via **projected views**: each dataset view guarantees its canonical columns
exist — aliasing a known rename or NULL-filling a missing one — while passing every
other column through untouched. So data written under an older *or* newer schema
still queries cleanly.

```text
eodhd> schema        # diff the declared schema against your on-disk columns
eodhd> reindex       # rebuild the query catalog after fetching new data
```

`schema` reports, per dataset, which canonical columns are present/missing and how
many extra columns are riding along — handy after a provider adds fields.

## Raw Data Lab (optional, LLM-backed)

A **grounded EDA copilot** for the pre-signal stage: state a question in plain
English and get back a *verified* answer with the exact query behind every number.
The rule is enforced in code, not the prompt — **the model never reports a number
it didn't compute** (each claim is backed by a read-only query the agent had to
write, run, and show). See [`lab/DESIGN.md`](lab/DESIGN.md) for the design.

```powershell
uv sync --extra lab
ollama pull qwen2.5-coder:7b        # free local model; fits a 12GB GPU
```

**From a one-liner to a full investigation:**

```text
eodhd> ask "which lanes have the worst dividend coverage, and why?"      # quick grounded Q&A
eodhd> agent microstructure "rank us_common by Amihud illiquidity this year"
eodhd> agent macro-strategist "relate uk_eu drawdowns to the 10Y-2Y curve and HY spreads"
eodhd> investigate "post-dividend volume patterns in us_common"          # generator→skeptic→reporter
eodhd> lab run coverage-audit --verify         # a saved playbook -> reproducible report
eodhd> lab agents · lab skills · lab config     # roster · playbooks · models/budget/keys
```

- **Grounded loop** — plan → SQL → **read-only guard** → execute → narrate; the
  answer shows each query and its result. The guard rejects anything that isn't a
  single `SELECT`/`WITH`.
- **A roster of lenses** — personas are files (`lab/personas/*.toml`): `analyst`,
  `auditor`, `macro-strategist`, `microstructure`, `event-study`, `hypothesizer`,
  `quant`, plus the `skeptic` and `reporter` — each scoped honestly to what EOD data
  (and, when crawled, the news corpus) supports. Skills (`lab/skills/*/SKILL.md`)
  are reusable playbooks. Add your own by dropping a file in.
- **Verify, don't trust** — `investigate` runs a **generator → skeptic → reporter**
  pipeline: the skeptic independently re-derives the numbers and votes
  `CONFIRMED / REFUTED / UNCERTAIN`, then a **reproducible Markdown report**
  (embedded queries + provenance) is written. One stage's model outage degrades to a
  note — never a crashed run.
- **Serious model where it matters** — the investigation roles default to **Opus**;
  the everyday `analyst` and mechanical `auditor` / `quant` run on the **free local**
  model. Grounding is temperature-independent (numbers are queried), so a strong
  reasoning model is used freely. A per-session budget + response cache bound spend;
  keys come from the environment, never config. Override any persona's `model` to
  trade cost for quality.
- **Macro join (FRED + EODHD)** — `macro fetch --run` pulls rates / curve / credit
  spreads / VIX / FX (`macro`), cross-country GDP / CPI / unemployment
  (`macro_country`), and index & FX levels (`macro_market`) into read-only views, so
  the macro personas can join real macro data to the equity tape by date instead of
  guessing.
- **Restricted Python (opt-in)** — set `[lab].allow_python` and the `quant` persona
  can run isolated Python (subprocess + timeout + no network) for stats and plots SQL
  can't express. A *trusted-local* convenience, **not** a hardened sandbox — off by
  default.

Optional and lazily imported — the core shell runs without the `lab` extra.

## Use your data from Claude Code / Cursor (MCP)

datacli ships an **MCP server** that exposes its read-only data tools — guarded SQL
over the DuckDB views, the schema, and the lane registry — so any MCP client can
query your local snapshot directly.

```powershell
uv sync --extra mcp
claude mcp add datacli -- uv run --extra mcp python mcp_server.py
```

Tools: `sql` (read-only `SELECT`/`WITH`, same guard as the lab), `describe_schema`,
`list_lanes`. The connection includes `news` when crawled and the macro views when
fetched.

## Backup (`sync`)

`sync` is a **push-only** backup of the data root — it never deletes or pulls.
Two backends: **Google Drive** (needs the `sync` extra and a one-time OAuth
client, see [`storage/GDRIVE_SETUP.md`](storage/GDRIVE_SETUP.md)) or a **local
directory** (no setup).

```powershell
uv sync --extra sync                                   # Drive backend only
uv run python eodhd/cli.py config set sync-backend local
uv run python eodhd/cli.py config set sync-local-dest "E:\backup\eodhd"
uv run python -m storage.cli                           # = sync status: what would be pushed (offline)
uv run python -m storage.cli push --run                # push (Drive: browser OAuth on first run)
```

Inside the shell the same is `sync`, `sync push --run`, `sync login`. Caches
(`cache/`, `probe_cache/`) are skipped unless `--with-caches`; progress is kept in a
manifest under `<data-root>/.sync/` so an interrupted push resumes.

## Configuration & where data lives

`config` (no argument) shows the resolved data root and where it came from, whether
an EODHD API key resolves, the registered lanes and the backup backend:

```text
                                 datacli config (eodhd)
┌───────────────┬───────────────────────────────────────────────────────────────────────┐
│ setting       │ value                                                                 │
├───────────────┼───────────────────────────────────────────────────────────────────────┤
│ data-root     │ D:\data\raw\eodhd  (config, exists)                                   │
│ config file   │ <repo>\datacli.toml                                                   │
│ EODHD_API_KEY │ ****cdef                                                              │
│ lanes         │ us_common, uk_eu, us_etf, index_ref, uk_eu_etf, uk_eu_index_ref, news │
│ sync backend  │ gdrive  -> datacli/eodhd                                              │
└───────────────┴───────────────────────────────────────────────────────────────────────┘
        set with:  config set <key> <value>   keys: data-root, sync-backend, sync-remote-root, sync-gdrive-secrets, sync-local-dest
```

**Data root.** Historically the raw EODHD snapshots (multiple GBs) lived in the
`btest` sibling repo, so the *default* is `../btest/data/raw/eodhd`; you almost
certainly want to set your own with `config set data-root <path>` (persisted in
the git-ignored `datacli.toml`) or, for a one-off, an environment variable:

```powershell
$env:EODHD_DATA_ROOT = "D:\somewhere\data\raw\eodhd"
```

**API key.** `EODHD_API_KEY` is read from, in order: the environment variable; the
Windows *user* environment (`setx EODHD_API_KEY <key>`, so it survives new shells);
`<repo>/configs/local/eodhd_api_key.txt` or `<repo>/local_cache/eodhd_api_key.txt`
(one line, git-ignored); an `EODHD_API_KEY=…` line in `./.env` or `<repo>/.env`
(the same file names one directory *above* the repo are also read, for setups
that predate this repo). It is never written to `datacli.toml`. `config` shows
`NOT SET` if nothing resolves.

**Other keys** (all environment variables): `FRED_API_KEY` for `macro fetch`;
`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` for the lab. See `datacli.example.toml` for
the full config template.

## Scheduled jobs (Windows)

Datacli can install daily or weekly workflows in Windows Task Scheduler. Windows
owns trigger timing; datacli keeps the exact workflow, immutable definition
snapshots, resource locks, and append-only run history under
`%LOCALAPPDATA%\datacli\profiles\<profile-id>\`.

Create a one-step job from PowerShell (the command after `--` keeps its own
arguments unchanged):

```powershell
.venv\Scripts\python.exe datacli.py schedule add morning-refresh --daily 06:00 -- eodhd refresh --fast --run
```

Create and inspect a multi-step draft before it becomes executable:

```powershell
.venv\Scripts\python.exe datacli.py schedule create morning --daily 06:00
.venv\Scripts\python.exe datacli.py schedule step add morning -- eodhd refresh --fast --run
.venv\Scripts\python.exe datacli.py schedule step add morning -- eodhd reindex
.venv\Scripts\python.exe datacli.py schedule step add morning -- sync push --run
.venv\Scripts\python.exe datacli.py schedule show morning
.venv\Scripts\python.exe datacli.py schedule enable morning
```

The same commands work inside the interactive shell without the
`.venv\Scripts\python.exe datacli.py` prefix. Useful management operations are
`schedule list`, `status`, `history`, `logs`, `test`, `run`, `pause`, `resume`,
`stop`, `edit`, `delete`, `reconcile`, and `doctor`; `schedule commands` shows
the allowlist.

For an atomic multi-step edit, run `schedule edit <job> --draft`, use
`schedule step add|remove|replace <draft-id> ...`, inspect it, then
`schedule enable <draft-id>`. A stale edit draft cannot overwrite a newer job
generation.

- `test` runs the stored definition in the foreground and returns its actual
  datacli run record. `run` only asks Windows to dispatch the installed task;
  acceptance is not completion.
- Jobs run as the current user with `InteractiveToken`: locked is supported,
  logged out is not. Defaults do not wake the computer, do not start on
  battery, do not stop active work on a later battery transition, and do not
  retry failures automatically.
- Google Drive jobs require an existing cached login. Scheduled execution never
  opens a browser; run `sync login` interactively first.
- `pause` affects future dispatches. `delete` writes a tombstone and preserves
  run history/snapshots; destructive history removal is a separate confirmed
  `purge` operation.
- `status` reports desired state, Windows observation, and datacli run history
  independently. It does not infer a successful run from a nominal trigger or
  Windows result alone.

## Architecture

```
datacli/
├─ datacli.py            the interactive shell (cmd2 + Rich REPL, tab-completion)
├─ eodhd/                the EODHD source plugin
│  ├─ cli.py             unified front door (status / refresh / qc / explore / …)
│  ├─ eodhd_datasets.py  the lane + dataset registry (single source of truth)
│  ├─ status_eodhd.py    as-of / staleness dashboard
│  ├─ report_eodhd_raw_quality.py   the QC engine (price-bearing lanes)
│  ├─ fetch_eodhd_*.py   per-lane fetchers  ·  fetch_eodhd_bulk.py  fast path
│  ├─ fetch_eodhd_news.py   the news day-crawler
│  ├─ explore_eodhd.py   DuckDB-backed describe / find / rows / coverage / sql
│  ├─ schema.py          versioned canonical schema + projected views
│  ├─ config.py          data-root resolution + datacli.toml
│  └─ _render.py         shared console + palette (one look for every command)
├─ macro/                the macro source (FRED + EODHD series, DuckDB views)
├─ llm/                  shared model layer (LiteLLM behind one interface, budget, cache, tiers)
├─ scoring/              news scoring: schemas (TOML), backends (vendor / llm / embed), runner, `score` CLI
├─ lab/                  the Raw Data Lab (personas, skills, grounded agent, pipeline)
├─ storage/              push-only backup (Google Drive / local) behind `sync`
├─ scheduler/            registry, definitions, locks, runner, journal, Windows adapter
├─ mcp_server.py         MCP server exposing sql / describe_schema / list_lanes
├─ scripts/blackbox.py   black-box scenario harness (see SCENARIOS.md)
└─ tests/                unit tests
```

Design principles:

- **Registry-driven, no hardcoding** — lanes/datasets are declared once in
  `eodhd_datasets.py`; `status`, `refresh`, `lanes` and the explorer iterate the
  registry (the QC engine keeps its own per-lane audit map).
- **State sidecars** — a per-ticker (per-day for news) `*_fetch_state.csv` acts as a
  fast index of coverage and fetch status, so `status` rarely has to open a parquet.
- **One visual language** — `_render.py` centralises the console, colours and
  glyphs; reference tables use a framed box, dashboards a lighter one, and both
  degrade to clean text under `NO_COLOR`.

## Development

```powershell
uv sync --extra dev --extra lab   # tests + the lab agents in one venv
uv run pytest -q                  # run the test suite (needs the dev extra)
uv run black . ; uv run isort .   # format
uv run mypy eodhd datacli.py      # type-check
```

> Combine extras: `uv sync --extra lab` alone drops the dev deps, so `pytest`
> isn't installed and `uv run pytest` won't run at all. Use
> `uv sync --extra dev --extra lab` for the full dev + agents environment.

**Black-box scenario harness** — `scripts/blackbox.py` drives the real commands as
subprocesses and checks their output; it's both a CI-style test and a slow-motion
demo. It runs against a **real local snapshot** (see [`SCENARIOS.md`](SCENARIOS.md)),
so on a fresh clone with an empty data root the data-dependent steps fail by design.

```powershell
uv run python scripts/blackbox.py --check     # assert + exit code
uv run python scripts/blackbox.py --demo      # slow-motion screencast
```

## Provenance

Extracted from the `btest` sibling repository (the `scripts/eodhd/` toolkit plus
the datacli shell). `btest` keeps the backtesting framework and its runtime data
adapters; this repo owns data **acquisition and exploration**. The raw snapshots
may still live in `btest` (hence the legacy default data root) and are reached via
the configurable data root above.
