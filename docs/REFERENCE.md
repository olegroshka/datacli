# datacli — full reference

The [README](../README.md) covers the concepts, a quickstart and what the
news-scoring experiments measured. This file is the exhaustive part: every
command, every configuration key, and the architecture.

---

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
| `finra status [--live] \| list \| qc` | The FINRA source's coverage (`--live` adds what FINRA has published), catalog, quality checks | `python -m finra.cli` |
| `sec status \| fetch [--dataset form13f\|adv] [--limit N] [--run] \| units \| qc` | The SEC source: Form 13F data sets (institutional holdings), listing, coverage, quality checks | `python -m sec.cli` |
| `positioning status \| build [--dataset short_ladder\|long_ladder\|holdings_inputs] [--run] \| qc [--dataset NAME]` | Derived positioning datasets: FIFO lot ladders over FINRA short interest and over SEC 13F holdings, and the 13F holdings inputs (offline; `build` is a dry run unless `--run`) | `python -m positioning.cli` |
| `score plan \| run --run \| status` | Schema-driven scores over the news corpus with a **local** model by default (`event_v1`: event type, summary, sentiment, per-symbol direction); paid models only with `--budget-usd` | `python -m scoring.cli` |

**Hits a provider — spends EODHD units (`$`) or needs a provider key:**

| Command | What it does | Direct entry point |
|---|---|---|
| `fetch` / `refresh [lanes] [--fast] [--run]` `$` | Download / top up data (dry-run unless `--run`) | `eodhd/cli.py refresh` |
| `probe TICKER…` `$` | Ad-hoc availability probe; caches raw payloads under `<data-root>/probe_cache/`, never touches lane outputs | `eodhd/cli.py probe` |
| `macro fetch [--run]` | Pull FRED (needs `FRED_API_KEY`) + EODHD macro series (`$`) | `python -m macro.cli fetch` |
| `sec fetch [--limit N] [--full] --run` | Download the SEC's Form 13F data-set archives (public, no key, needs `SEC_USER_AGENT`; 20 to 100 MB each) | `python -m sec.cli fetch` |
| `finra fetch [--dataset short_volume\|weekly_flow\|short_interest] [--from D] [--to D] [--limit-days N] [--run]` | Daily short sale volume from FINRA's public files (free, no key; `--transport api` uses the Query API), or the weekly ATS/OTC flow (Query API, needs credentials) | `python -m finra.cli fetch` |
| `finra probe auth \| metadata \| partitions <dataset>` | Read-only calls against the FINRA Query API (credentials optional for public datasets) | `python -m finra.cli probe` |

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
the [status dashboard](../README.md#datacli). On an empty root it says so and points at the
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

See [`eodhd/README.md`](../eodhd/README.md) for the full acquisition runbook
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
  [`eodhd/NEWS_SCORING_DESIGN.md`](../eodhd/NEWS_SCORING_DESIGN.md).
- **Know its quirks** before modelling on it: the vendor sentiment is a coarse
  VADER-style score, symbol tagging is US-biased (an EU issuer's US line collects
  most of the tags), ~14 % of articles carry no symbol at all, and daily volume is
  not stationary across years. All of it is documented, with numbers, in
  [`eodhd/EODHD_NEWS_SENTIMENT_FINDINGS.md`](../eodhd/EODHD_NEWS_SENTIMENT_FINDINGS.md).

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
`catalog` once reindexed, `macro` / `macro_country` / `macro_market` once
fetched, and `finra_short_volume` / `finra_weekly_flow_symbol` /
`finra_short_interest` / `finra_fails_to_deliver` once the FINRA source is fetched,
`positioning_short_ladder` once `positioning build --run` has run,
`positioning_long_ladder` once `positioning build --dataset long_ladder --run`
has run, and `positioning_factors`).
Every EODHD view
carries a `lane` column:

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
write, run, and show). See [`lab/DESIGN.md`](../lab/DESIGN.md) for the design.

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
- **Positioning join (FINRA)** — `finra fetch --run` pulls Reg SHO daily short sale
  volume (consolidated NMS, one row per symbol per trade date) into
  `finra_short_volume`, with `eodhd_code` mapping FINRA's SIP spelling to EODHD
  tickers (`BRK/B` → `BRK-B`) and `short_ratio` ready to join to `prices` on
  `eodhd_code = ticker AND date`. Public files, no key. `finra fetch --dataset
  weekly_flow --run` adds the weekly ATS (dark pool) and OTC market-maker flow
  per symbol and venue (`finra_weekly_flow`, `finra_weekly_flow_symbol`), published
  by FINRA three (Tier 1) or five (Tier 2) weeks after the week, so join it on
  `published_at`. `finra fetch --dataset short_interest --run` adds the twice-monthly
  consolidated short interest (`finra_short_interest`, published seven business days
  after settlement, and `finra_short_interest_float` over EODHD shares outstanding).
  `finra fetch --dataset fails_to_deliver --run` adds the SEC's CNS fails to deliver
  per settlement date and CUSIP (`finra_fails_to_deliver`); the SEC requires a
  declared contact in `SEC_USER_AGENT`. See `docs/FINRA_SOURCE_DESIGN.md` and
  `docs/FINRA_CUT2_PLAN.md`.
- **13F holdings (SEC)** — `sec fetch --run` stores the SEC's Form 13F data sets as
  published (every column a string, one parquet per archive and table) and exposes
  `sec_13f_holdings`: one row per reported position with the filing's `filing_date`
  (the point-in-time column), `period`, manager name and `crd_number`, `cusip`,
  `value_usd` (the reported value in dollars: the form switched from thousands
  to dollars for filings from 2023-01-03, and each filing is classified against
  the other filers because some lagged; `units_evidence` says how), `shares`,
  `put_call`. Amendments are not resolved there; `sec_13f_holdings_effective`
  (and `sec_13f_filings_effective`) applies the rule: per manager and period the
  latest original or `RESTATEMENT` filing is the base and `NEW HOLDINGS`
  amendments filed on or after it are added, with `effective_filing_date` as the
  point-in-time column of the resolved set. Plus `sec_13f_submission` and
  `sec_13f_coverpage`. The archives are large: always filter. `sec fetch --dataset
  adv --run` adds the monthly Form ADV adviser reports (`sec_adv_advisers`: CRD,
  names, regulatory assets, whether the adviser runs hedge funds) and
  `sec_13f_manager_cohort`, each 13F filing joined to the latest ADV snapshot
  dated before its filing date, by the cover page's CRD (filings from 2023) or,
  through `sec_13f_manager_match`, by the ADV adviser's CIK or a unique
  normalised name (`match_kind` says which; ambiguous names match nothing).
- **CUSIP map (derived)** — `positioning_cusip_map` gives dated `(cusip, eodhd_code)` pairs
  from the fails-to-deliver files, the bridge from `sec_13f_holdings.cusip` to `prices`.
- **Short ladder (derived)** — `positioning build --run` runs a FIFO lot ladder over
  the twice-monthly short interest and writes `positioning_short_ladder`: per symbol
  and settlement date, the short `inventory` and `flow` on a split-neutral share
  basis, `wavg_age_days` (how long the open short has been held), `cost_basis` and
  `profit_pct` (short sellers' unrealised return; negative = under water), with
  `published_at` to ASOF-join on. Mask rows with `seed_share > 0.5`: the age of the
  first observation is unknown. A rebuild replaces only changed settlement dates and
  records a changed past date as a restatement. Two caveats are built in: the
  vendor's splits table also lists spin-off and merger price adjustments, so share
  counts are rescaled only by the entries the short interest reports confirm
  (`quantity_factor` vs `price_factor`); and `inventory` is on a split-neutral basis
  (compare within a symbol, never across). The `lane` column says where a symbol's
  prices come from (`us_extended` includes delisted names).
- **Long ladder (derived)** — `positioning build --dataset long_ladder --run` runs the
  same ladder on the long side over the amendment-resolved 13F holdings and writes
  `positioning_long_ladder`: per CUSIP, quarter end (`period`) and aggregate (`all` =
  every 13F filer, `cohort` = filers whose Form ADV says hedge funds, else private
  funds; the flags exist only for filings from 2023). `flow` is the quarter's trading
  summed over managers who filed both quarters; `drift_shares` is what came or went
  with a manager entering or leaving the panel and is absorbed into the open lots
  without a trade, so `inventory` equals the aggregate's level on the split-neutral
  basis. Filings later than 60 days after the period are left out, which keeps
  `published_at` between 45 and 60 days after the period; the lot price is the
  quarter's mean close. `qc --dataset long_ladder` reconciles every stored level with
  the sum of effective holdings. Shared-discretion rows (`DFND`) are counted as
  filed, so a widely held name's level can exceed its shares outstanding.
- **Holdings inputs (derived)** — `positioning build --dataset holdings_inputs --run`
  writes `positioning_holdings_inputs` from the same 13F panel: per CUSIP, quarter end
  and aggregate, `long_fund_weight` (the position's weight in each holder's 13F book,
  summed over holders), `long_conc` (Herfindahl of shares across holders), `best_ideas`
  (holders for whom it is a top-10 weight, normalised to sum to 1 per period) and
  `n_holders`. No prices needed.
- **Renamed tickers** — `positioning_symbol_alias` lists tickers whose CUSIP reappears in
  the fails-to-deliver files under a later ticker (a rename; the vendor keeps the price
  history under the new one). The short ladder prices such a symbol's reports from the
  alias.
- **Factor view** — `positioning_factors` gives, per US common ticker and day from
  2018, `reversal_21d`, `momentum_12_1`, `specific_risk_63d` (annualised volatility
  of the return in excess of the sector median) and `short_interest_ratio` (latest
  short interest over shares outstanding published before the day) and
  `price_quality_flag` (true on a day whose vendor bar fails a quality rule, or for
  the year after such a bar: a non-positive or inconsistent bar, a placeholder close,
  a one-day move above 300 percent). Raw values, known after the close of `date`,
  computed on the fly: always filter by date or ticker, and filter the flag out before
  using the factors. `positioning qc` lists the ladder symbols with failing bars.
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
`list_lanes`. The connection includes `news` when crawled, the macro views when
fetched, and `finra_short_volume` when the FINRA source is fetched.

## Backup (`sync`)

`sync` is a **push-only** backup of the data root — it never deletes or pulls.
Two backends: **Google Drive** (needs the `sync` extra and a one-time OAuth
client, see [`storage/GDRIVE_SETUP.md`](../storage/GDRIVE_SETUP.md)) or a **local
directory** (no setup).

It covers the eodhd data root and, when they exist, the `macro` and `finra`
roots as separate units: each keeps its own manifest under its `.sync/` and
goes to its own folder next to `remote_root` (`datacli/macro`, `datacli/finra`;
the `local` backend uses siblings of `local_dest`). `status`, `push` and
`reconcile` report every unit; the exit code is the worst of them.

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
`FINRA_CLIENT_ID` + `FINRA_API_KEY` (the client secret) for the FINRA Query API,
read from the environment or the Windows user environment and optional for the
public short volume files; `SEC_USER_AGENT` (a declared name and contact email,
required by the SEC's access policy) for the fails-to-deliver files; `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` for the lab.
See `datacli.example.toml` for the full config template.

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

Use `schedule --help` for the lifecycle overview and
`schedule <operation> --help` for behavior, safety notes, defaults, and complete
examples. In the interactive shell, tab completion covers every management
operation and option, known job/draft/profile IDs, draft step actions/indexes,
filesystem paths, and every allowlisted command family, verb, and supported
argument.

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
│  ├─ fetch_eodhd_us_extended_*.py   the us_extended lane: shorted US names the other lanes do not price, delisted included
│  ├─ explore_eodhd.py   DuckDB-backed describe / find / rows / coverage / sql
│  ├─ schema.py          versioned canonical schema + projected views
│  ├─ config.py          data-root resolution + datacli.toml
│  └─ _render.py         shared console + palette (one look for every command)
├─ macro/                the macro source (FRED + EODHD series, DuckDB views)
├─ finra/                the FINRA source (Query API client, public daily files, short volume store, views)
├─ sec/                  the SEC source (Form 13F data sets: listing, download, raw store, views)
├─ positioning/          derived datasets (split-neutral basis, FIFO lot ladder, short and long ladder stores + views)
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
demo. It runs against a **real local snapshot** (see [`SCENARIOS.md`](../SCENARIOS.md)),
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
