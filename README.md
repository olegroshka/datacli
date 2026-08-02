# datacli

**An interactive data-operations shell for market data** — download, refresh,
quality-check and explore raw provider snapshots from one cohesive, colour-coded
terminal UI.

datacli turns a directory of raw parquet/CSV snapshots into a queryable,
auditable dataset. It ships a registry-driven acquisition toolkit for
[EODHD](https://eodhd.com) (prices, dividends, splits, fundamentals across US and
UK/EU equities, ETFs and indices) and a DuckDB-backed explorer for fast, ad-hoc
questions — all behind a single REPL with tab-completion.

```text
                                     EODHD status · as-of 2026-07-11 · stale >7d

 lane        dataset            last_data    coverage     fetched         rows   pairs   age   flag      state
 ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
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
  add one entry and every command (status, refresh, QC) picks it up. No hardcoded
  dataset names.
- **Freshness at a glance** — a colour-coded *as-of* dashboard that measures
  staleness against the correct anchor per dataset kind (prices vs. events).
- **Actionable QC** — a triage view that flags structural issues and tells you the
  fix (`targeted_rerun` vs. `full_refresh`), scoped by lane and dataset.
- **Ask questions, not SQL** — `describe` / `find` / `rows` / `coverage`, plus a raw
  `sql` escape hatch, all over a warm DuckDB connection.
- **Schema-versioned & evolution-safe** — projected views NULL-fill or rename-alias
  columns so queries stay stable as a provider's schema drifts.
- **Fast refresh** — bulk end-of-day endpoints turn an hours-long per-ticker pull
  into minutes.
- **Cohesive by design** — every command shares one console and palette: dataset
  hues, severity glyphs, freshness colours, and degrades cleanly under `NO_COLOR`
  or a pipe.

## Contents

- [Requirements](#requirements) · [Install](#install) ·
  [Quickstart](#quickstart-point-datacli-at-your-own-data)
- [The shell](#the-shell) · [Command reference](#command-reference)
- [Data acquisition](#data-acquisition-status--fetch--qc) ·
  [Exploring the data](#exploring-the-data)
- [Schema & versioning](#schema--versioning) ·
  [Raw Data Lab](#raw-data-lab-optional-llm-backed) ·
  [Configuration](#configuration--where-data-lives)
- [Architecture](#architecture) · [Development](#development) ·
  [Provenance](#provenance)

## Requirements

- **Python ≥ 3.11**
- [**uv**](https://docs.astral.sh/uv/) for environment and dependency management
- An **EODHD API key** (for fetching; exploring existing snapshots needs no key)

## Install

```powershell
uv sync --extra dev
```

## Quickstart: point datacli at your own data

Already have EODHD raw snapshots on disk? Three steps take you from clone to an
answered question:

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
EODHD_DATA_ROOT env var   >   datacli.toml [eodhd].data_root   >   ../btest default
```

## The shell

```powershell
uv run python datacli.py
```

```text
data>  sources                  # list data sources
data>  source eodhd             # enter a source context -> eodhd>
eodhd> status                   # colour-coded as-of dashboard (all lanes)
eodhd> status us_common         # ... scoped to one lane
eodhd> fetch --fast --run       # bulk refresh (minutes, not hours)
eodhd> qc us_common splits      # quality triage, drilled into one dataset
eodhd> describe VAR.OL          # everything about one ticker
eodhd> source macro             # switch to the macro source -> macro>
macro> status                   # FRED + EODHD macro series and their coverage
```

`status` reports the **current** source's datasets only — each source owns its own
data — so it prints a pointer to the peer sources underneath. `macro` is a
first-class source alongside `eodhd`; `macro status` is just a shortcut that saves
you from `source macro` first.

`sources` lists the available adapters and the commands each exposes:

```text
                                           data sources
┌─────────┬───────────────────────────────────────────┬───────────────────────────────────────────┐
│ source  │ summary                                   │ commands                                  │
├─────────┼───────────────────────────────────────────┼───────────────────────────────────────────┤
│ eodhd   │ US/UK-EU equities, ETFs, indices,         │ status fetch qc lanes probe describe find │
│         │ fundamentals                              │ rows coverage sql config schema reindex   │
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
- **`help`** lists commands, **`clear`** wipes the screen, **`quit`** (or **`exit`**)
  leaves.

## Command reference

Run any command with `--help` for its full options.

**Data & exploration** — no model or API key required:

| Command | What it does |
|---|---|
| `status [lane]` | As-of dashboard: what data exists and how fresh it is |
| `fetch [lanes] [--fast] [--run]` | Download / refresh data (dry-run unless `--run`) |
| `qc [lane] [dataset]` | Raw-data quality triage with recommended fixes |
| `lanes` | List registered lanes, datasets, and their fetchers |
| `probe TICKER…` | Ad-hoc availability probe (read-only; no writes) |
| `describe TICKER` | Everything about one ticker, across datasets |
| `find PATTERN` | Locate a ticker (lane / exchange / datasets) |
| `rows TICKER DATASET` | Show the actual rows for a ticker in a dataset |
| `coverage TICKER` | Do the datasets cover a ticker equally? |
| `sql "<query>"` | Raw read-only DuckDB over the dataset views |
| `schema` | Declared schema version + drift vs. on-disk data |
| `reindex` | (Re)build the fast query catalog after new data |
| `config [set data-root <path>]` | Show / edit configuration |
| `sync [push --run \| login]` | One-way backup of the data root to cloud storage (Google Drive; dry-run unless `--run`) |
| `macro status \| list \| fetch` | The macro source (FRED + EODHD series) — see `sources` |

**Agentic — the [Raw Data Lab](#raw-data-lab-optional-llm-backed)** ✦ *(needs a model — see note below):*

| Command | What it does |
|---|---|
| `ask "<question>"` ✦ | Grounded natural-language Q&A from the default persona |
| `agent <persona> "<q>"` ✦ | Ask a named persona (`macro-strategist`, `microstructure`, …) |
| `investigate "<topic>"` ✦ | Multi-agent generator → skeptic → reporter; writes a verified report |
| `lab run <skill> [--verify]` ✦ | Run a saved EDA playbook → reproducible report |
| `lab agents` · `lab skills` · `lab config` | Roster · playbooks · models/budget/keys (`config` needs no model) |

Data & exploration commands run in the shell or directly as
`uv run python eodhd/cli.py <command>`; the agentic commands are
`uv run python -m lab.cli <command>` (and work unprefixed inside the shell).

> ✦ **Needs a model.** Install the lab (`uv sync --extra lab`) and have at least one
> model available: a **local Ollama** model (free — `ollama pull qwen2.5-coder:7b`)
> and/or an `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` in your environment. Serious
> personas default to **Opus**; the everyday `analyst` and mechanical `auditor` /
> `quant` run **free on local**. Run **`lab config`** to see exactly what's configured
> and available.

## Data acquisition: status · fetch · qc

`status` measures staleness against the correct freshness anchor for each dataset
kind — the last *bar* for prices, the query *coverage* ceiling for events (so a
legitimately future-dated dividend is never mistaken for an anomaly). See the
dashboard at the [top of this page](#datacli).

`fetch` prints an ordered plan and does nothing until you add `--run` (it hits a
paid API). `--fast` uses bulk end-of-day endpoints — one call per exchange instead
of one per ticker, turning hours into minutes.

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
kind-hued dot and the fetcher that populates it:

```text
                                            EODHD lanes (6)

 lane            region / class    dataset          fetcher
 ─────────────────────────────────────────────────────────────────────────────────────────────────────
 us_common       US / common       universe         (derived from another stage)
                                   ● prices         fetch_eodhd_us_prices.py
                                   ● dividends      fetch_eodhd_us_dividends.py
                                   ● splits         fetch_eodhd_us_splits.py
                                   ● fundamentals…  fetch_eodhd_us_fundamentals.py --update

 uk_eu           UK/EU / common    universe         (derived from another stage)
                                   ● prices         fetch_eodhd_eu_prices.py
                                   …
```

See [`eodhd/README.md`](eodhd/README.md) for the full acquisition runbook
(resume model, bulk refresh, fundamentals, per-lane manifests).

## Exploring the data

**Three ways to ask, in increasing flexibility:** entity verbs
(`describe` / `find` / `rows` / `coverage`) for instant structured answers with no
SQL; a raw **`sql`** escape hatch when you want full control; and the
natural-language **[Raw Data Lab](#raw-data-lab-optional-llm-backed)** when you'd
rather state the question than write the query. Everything runs over the raw parquet
via a warm DuckDB connection, and each verb also works as a direct
`eodhd/cli.py <verb>`.

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

`sql` is the raw read-only escape hatch, running DuckDB against views named
`prices`, `dividends`, `splits`, `fundamentals` (and their `*_state` sidecars).
Every view carries a `lane` column:

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
`SCHEMA_VERSION`. Queries stay stable as data evolves via **projected views**: each
dataset view guarantees its canonical columns exist — aliasing a known rename or
NULL-filling a missing one — while passing every other column through untouched. So
data written under an older *or* newer schema still queries cleanly.

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
  supports. Skills (`lab/skills/*/SKILL.md`) are reusable playbooks. Add your own by
  dropping a file in.
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
`list_lanes`. The connection includes the macro views when fetched.

## Configuration & where data lives

`config` (no argument) shows the resolved data root and where it came from, plus
API-key presence and the registered lanes:

```text
                                 datacli config (eodhd)
┌───────────────┬───────────────────────────────────────────────────────────────────────┐
│ setting       │ value                                                                 │
├───────────────┼───────────────────────────────────────────────────────────────────────┤
│ data-root     │ D:\data\raw\eodhd  (config, exists)                                   │
│ config file   │ <repo>\datacli.toml                                                   │
│ EODHD_API_KEY │ ****cdef                                                              │
│ lanes         │ us_common, uk_eu, us_etf, index_ref, uk_eu_etf, uk_eu_index_ref       │
└───────────────┴───────────────────────────────────────────────────────────────────────┘
                         set with:  config set data-root <path>
```

The raw EODHD snapshots (multiple GBs) live in the `btest` sibling repo by default,
so the fetchers read/write `../btest/data/raw/eodhd`. Override it with
`config set data-root <path>` (persisted in the git-ignored `datacli.toml`) or, for
a one-off, an environment variable:

```powershell
$env:EODHD_DATA_ROOT = "D:\somewhere\data\raw\eodhd"
```

## Architecture

```
datacli/
├─ datacli.py            the interactive shell (cmd2 + Rich REPL, tab-completion)
├─ eodhd/                the EODHD source plugin
│  ├─ cli.py             unified front door (status / fetch / qc / explore / …)
│  ├─ eodhd_datasets.py  the lane + dataset registry (single source of truth)
│  ├─ status_eodhd.py    as-of / staleness dashboard
│  ├─ report_eodhd_raw_quality.py   the QC engine
│  ├─ fetch_eodhd_*.py   per-lane fetchers  ·  fetch_eodhd_bulk.py  fast path
│  ├─ explore_eodhd.py   DuckDB-backed describe / find / rows / coverage / sql
│  ├─ schema.py          versioned canonical schema + projected views
│  ├─ config.py          data-root resolution + datacli.toml
│  └─ _render.py         shared console + palette (one look for every command)
└─ tests/                unit tests
```

Design principles:

- **Registry-driven, no hardcoding** — lanes/datasets are declared once in
  `eodhd_datasets.py`; commands iterate the registry.
- **State sidecars** — a per-ticker `*_fetch_state.csv` acts as a fast index of
  coverage and fetch status, so `status` rarely has to open a parquet.
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

> Combine extras: `uv sync --extra lab` alone drops the dev deps (pytest), which
> makes `uv run pytest` skip the DuckDB-backed tests. Use
> `uv sync --extra dev --extra lab` for the full dev + agents environment.

**Black-box scenario harness** — `scripts/blackbox.py` drives the real commands as
subprocesses and checks their output; it's both a CI-style test and a slow-motion
demo. See [`SCENARIOS.md`](SCENARIOS.md).

```powershell
uv run python scripts/blackbox.py --check     # assert + exit code (12/12 pass)
uv run python scripts/blackbox.py --demo      # slow-motion screencast
```

## Provenance

Extracted from the `btest` sibling repository (the `scripts/eodhd/` toolkit plus
the datacli shell). `btest` keeps the backtesting framework and its runtime data
adapters; this repo owns data **acquisition and exploration**. The raw snapshots
remain in `btest` by default and are reached via the configurable data root above.
