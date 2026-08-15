# EODHD Workflow Runbook

This directory contains the operational fetchers, the lane registry, and the
factual manifests for the datacli EODHD data lanes. The root
[`README.md`](../README.md) is the product tour; this file is the operator's
runbook: how to fill, refresh, resume, and audit.

All commands run from the **repo root** (`datacli/`). Examples are PowerShell.

## 0. Prerequisites: data root + API key

```powershell
uv sync --extra dev
uv run python eodhd/cli.py config set data-root "D:\data\raw\eodhd"   # where parquet lives
$env:EODHD_API_KEY = "<your key>"      # this shell; persist with: setx EODHD_API_KEY <your key>
uv run python eodhd/cli.py config      # shows the resolved root and whether the key resolves
```

- **Data root** resolution order: `EODHD_DATA_ROOT` env var → `datacli.toml
  [eodhd].data_root` → legacy default `../btest/data/raw/eodhd`.
- **API key** resolution order (`fetch_eodhd_eu_fundamentals._get_api_key`):
  `EODHD_API_KEY` env var → Windows user environment (`setx`) →
  `<repo>/configs/local/eodhd_api_key.txt` or `<repo>/local_cache/eodhd_api_key.txt`
  → `EODHD_API_KEY=…` in `./.env` or `<repo>/.env` (the same file names one level
  above the repo are also read, for pre-datacli setups). It is never stored in
  `datacli.toml`. Only fetching needs it; `status`/`qc`/explore work without one.
- **Quota / cost.** Every call costs API units (fundamentals ≈ 10/firm, prices and
  events 1/ticker-window, news 5/page, bulk 100 per exchange × day × kind — so a
  routine `--fast` top-up is ≈ 4k units per day behind across the 13 exchanges).
  The account this was built against has a 100,000-unit daily limit. `refresh` is
  dry-run unless `--run`, prints its plan first, and every dry-run is free (the
  `--fast` dry-run prints the planned bulk calls and units), so nothing is spent
  by accident.

## 1. Quick start — unified CLI

`cli.py` is the single front door. It is driven by the lane registry in
`eodhd_datasets.py`, so it never hardcodes lane or file names. `--help` prints the
command list **and the lifecycle map** (setup → first fill → routine → verify →
index → explore).

> **Interactive shell:** `datacli.py` (repo root) is a cmd2/Rich REPL over all
> data sources. `uv run python datacli.py`, then `source eodhd` and run
> `status`, `fetch --fast --run` (shell `fetch` == CLI `refresh`), `qc`,
> `reindex`, `describe …` — it reuses this CLI.

```powershell
uv run python eodhd/cli.py --help          # all commands + lifecycle
uv run python eodhd/cli.py status          # what data we have, as of when
uv run python eodhd/cli.py status --write  # + regenerate <data-root>/STATUS.md and STATUS.json
uv run python eodhd/cli.py lanes           # registered lanes / datasets / universe sources / fetchers
uv run python eodhd/cli.py refresh         # show the refresh plan (no fetch)
uv run python eodhd/cli.py refresh --run   # execute: prices + events + news top-up, all lanes (per-ticker; slow)
uv run python eodhd/cli.py refresh --fast --run   # FAST top-up: bulk endpoints (minutes) + news top-up
uv run python eodhd/cli.py refresh us_common --run
uv run python eodhd/cli.py refresh --datasets fundamentals --run   # weekly fundamentals (--update)
uv run python eodhd/cli.py probe AAPL MSFT NVDA   # ad-hoc availability check (spends units)
```

`refresh` is **dry-run by default** and only fetches with `--run`. The individual
`fetch_eodhd_*.py` scripts below remain the ground truth and are still the way to
do windowed or otherwise unusual pulls.

## 2. First fill (empty data root)

The order matters. The **common-stock lanes** (`us_common`, `uk_eu`) have no
universe fetcher: their price/dividend/split fetchers read
`<lane>/coverage_summary.csv`, which only the **fundamentals** stage writes.
`refresh` detects a missing coverage file — it runs fundamentals first when
selected, otherwise skips the per-ticker steps and prints the exact command — but
you should still run the stages deliberately, because a full first fill is hours
of wall-clock and roughly two quota-days.

```powershell
# 1. common-stock lanes: fundamentals (writes coverage_summary.csv), then prices/events
uv run python eodhd/cli.py refresh us_common uk_eu --datasets fundamentals --run
uv run python eodhd/cli.py refresh us_common uk_eu --run

# 2. ETF / index lanes bootstrap themselves: universe step, then prices/events
uv run python eodhd/cli.py refresh us_etf index_ref uk_eu_etf uk_eu_index_ref --run

# 3. news corpus backfill (2021 -> today; ~3 h, ~7 GB, ~28k units). Deliberately NOT part of refresh.
#    No dry-run: smoke-test with --limit-days 3 first if you like.
uv run python eodhd/fetch_eodhd_news.py

# 4. verify, index, explore
uv run python eodhd/cli.py status
uv run python eodhd/cli.py qc
uv run python eodhd/cli.py reindex
uv run python eodhd/cli.py describe AAPL.US
```

Budget (order of magnitude, from the manifests): fundamentals ≈ 6k US + ≈ 7.6k UK/EU
firms × ~10 units ≈ 140k; prices/events one call per ticker per dataset; news
5,511 pages × 5 units ≈ 28k (measured). That is roughly two quota-days in total.
Every stage is resumable — an interrupted run is simply re-run without
`--full-refresh`.

## 3. Point the tool at your data, then explore it

```powershell
uv run python eodhd/cli.py config                       # show resolved root + source + key status
uv run python eodhd/cli.py config set data-root "D:\data\raw\eodhd"
uv run python eodhd/cli.py reindex                       # build the query catalog
```

Ad-hoc queries over the raw parquet (DuckDB under the hood, no SQL required):

```powershell
uv run python eodhd/cli.py describe VAR.OL     # which datasets cover a ticker, how far
uv run python eodhd/cli.py find VAR            # fuzzy ticker search across datasets
uv run python eodhd/cli.py rows VAR.OL dividends           # latest rows, narrow columns
uv run python eodhd/cli.py rows VAR.OL prices --cols "*"   # ... or every column
uv run python eodhd/cli.py coverage VAR.OL     # per-dataset coverage windows
uv run python eodhd/cli.py sql "SELECT count(*) FROM dividends"
uv run python eodhd/cli.py sql "SELECT date, count(*) FROM news GROUP BY 1 ORDER BY 1 DESC LIMIT 7"
```

`schema` diffs the declared column contract (`schema.py`, versioned) against your
on-disk columns; **projected views** NULL-fill or rename-alias so queries stay
stable as the provider's columns evolve. **`describe` and `find` read the catalog
that `reindex` builds — re-run `reindex` after any fetch** or they report the
pre-fetch numbers. The news lane is day-keyed, so the ticker verbs skip it; use `sql`.

## 4. What lives where

- `README.md` (this file): operator-facing runbook for how to fill, resume, and audit the EODHD fetchers.
- `cli.py`: unified CLI (`status` / `refresh` / `qc` / `probe` / `lanes` / `config` / `schema` / `reindex` and the explore verbs `describe` / `find` / `rows` / `coverage` / `sql`).
- `eodhd_datasets.py`: the lane/dataset registry — single source of truth; add a lane or dataset here.
- `status_eodhd.py`: as-of / staleness reporter; writes `<data-root>/STATUS.md` + `STATUS.json` with `--write`.
- `report_eodhd_raw_quality.py`: the QC engine behind `qc` (price-bearing lanes; keeps its own per-lane audit map).
- `explore_eodhd.py`: DuckDB views + the explore verbs; `schema.py`: the versioned canonical schema.
- `fetch_eodhd_bulk.py`: the fast path — bulk end-of-day refresh (one call per exchange) for prices/dividends/splits; used by `refresh --fast`.
- `fetch_eodhd_news.py`: the news day-crawler (backfill + top-up).
- `fundamentals_refresh_common.py`: shared incremental-refresh helpers for the fundamentals fetchers (target selection, state sidecar, earnings-calendar lookup).
- `FUNDAMENTALS_REFRESH_DESIGN.md`: design notes for the fundamentals refresh (why append-only was a problem, the fix).
- `EODHD_*_MANIFEST.md`: per-lane factual inventories of scope, local artefacts, and observed counts (dated).
- `EODHD_NEWS_SENTIMENT_FINDINGS.md`: what the subscription exposes for news / sentiment (measured live), the `news` lane design, and the backfill result.
- `NEWS_ROADMAP.md`: the ordered plan for the news lane and refresh improvements (derived panel, issuer mapping, own scoring, refresh fixes, gap-fill).
- `NEWS_SCORING_DESIGN.md`: design request for the pluggable, schema-driven scoring layer over the corpus (brainstorm → decision → build).
- `fetch_eodhd_*.py`: the actual fetchers.

## 5. Current EODHD lane map

### US

- Common-stock lane: `EODHD_US_COMMON_STOCK_DATA_MANIFEST.md`
- ETF lane: `EODHD_US_ETF_DATA_MANIFEST.md`
- Index / benchmark lane: `EODHD_INDEX_REF_DATA_MANIFEST.md`

### UK/EU

- Common-stock lane: `EODHD_UK_EU_DATA_MANIFEST.md`
- ETF lane: `EODHD_UK_EU_ETF_DATA_MANIFEST.md`
- Index / benchmark lane: `EODHD_UK_EU_INDEX_REF_DATA_MANIFEST.md`

### Global

- News / sentiment lane: `EODHD_NEWS_SENTIMENT_FINDINGS.md`

## 6. Resume model

The fetchers are designed to be rerun normally.

Continuation state is persisted in the per-lane sidecars:

- prices: `prices_fetch_state.csv`
- dividends: `dividends_fetch_state.csv` plus `dividends_fetch_audit.csv`
- splits: `splits_fetch_state.csv` plus `splits_fetch_audit.csv`
- fundamentals: `fundamentals_fetch_state.csv` (per firm)
- news: `news_fetch_state.csv` (one row per crawled UTC day)

Normal reruns should **not** use `--full-refresh`.

On a normal rerun, the scripts will:

- skip pairs already covered through the requested `--to` bound,
- continue incremental tails for pairs with local history,
- preserve explicit `empty` states for tickers where the provider returned no dividend/split history,
- and merge new output with existing parquet files.

Use `--full-refresh` only when you intentionally want to rebuild an output from scratch.

## 7. Fast refresh (bulk end-of-day)

The per-ticker fetchers make one HTTP call per ticker — tens of thousands of
calls, hours of wall-clock — because they're request-bound (incremental shrinks
the *data* per call, not the *number* of calls). For a routine daily/weekly
top-up, use the bulk path instead:

```powershell
uv run python eodhd/cli.py refresh --fast --run          # all lanes: prices/divs/splits via bulk + news top-up
uv run python eodhd/cli.py refresh --fast us_common      # dry-run, one lane
uv run python eodhd/cli.py refresh --fast --days 3 --run # only fill the last 3 days
```

It pulls a whole exchange's latest day(s) in a single `/eod-bulk-last-day/{EXCHANGE}`
call (shared across lanes on the same exchange), filters to each lane's universe,
and **append-merges** only genuinely-new rows — existing rows are never rewritten,
so multiple dividends on one ex-date are preserved. State advances only to each
pair's actual newest bar (never past real data), so nothing is ever skipped. The
news top-up (capped, newest days first) runs after the bulk step.

Caveats / when to still use the per-ticker path:
- **First fill:** `--fast` only tops up lanes that already have a state sidecar. On
  an empty root it reports `[no state sidecar]` for every lane and fetches nothing
  (except the news top-up). Use §2.
- **Adjustments:** bulk appends bars with the provider's current adjustment; it
  does not re-adjust historical `adjusted_close` after a split. Tickers that split
  are reported — run a periodic per-ticker `--full-refresh` for those.
- **Large gaps:** a pair more than `--days` behind is reported and skipped (never
  hole-punched); use the normal `refresh --run` to backfill it.
- **New listings / universe:** `--fast` updates the existing universe; run the
  universe fetchers (or a normal refresh) to discover newly listed tickers.
- **Fundamentals:** not part of `--fast`; use `refresh --datasets fundamentals --run`.
- **Targeted reruns:** `--fast` cannot be combined with `--tickers/--to/--limit/--full-refresh/--no-universe`
  (the bulk path has no per-ticker window); drop `--fast` for those.

## 8. Fundamentals refresh

Fundamentals are **not** append-only anymore. The fetchers support:

- *(default)* backfill — fetch only firms not yet present (initial load / resume),
- `--update` — refresh firms that reported since the last pull (via the EODHD
  `/calendar/earnings` endpoint) plus any new firms; falls back to `--stale-days N`
  if the calendar is unavailable or `--no-calendar` is passed,
- `--full-refresh` — re-fetch every firm (restatement sweep / hard rebuild).

Each run writes a per-firm `fundamentals_fetch_state.csv` sidecar (last `fetched_at`,
`latest_filing_date`, `latest_statement_date`, `n_quarters`), which the status tool
uses for freshness. Re-fetched firms are upserted into `fundamentals_quarterly.parquet`
(merge on `ticker, exchange, statement, date`), so new quarters and restatements land
correctly. The fundamentals stage also writes `coverage_summary.csv`, the file the
common-stock price/event fetchers select their universe from (`both_60q == 1`).

```powershell
# Routine incremental refresh (calendar-targeted) via the CLI. Note the difference:
uv run python eodhd/cli.py refresh --datasets fundamentals --run   # fundamentals ONLY (weekly routine)
uv run python eodhd/cli.py refresh --with-fundamentals --run       # default kinds (per-ticker prices/events + news) PLUS fundamentals

# Or a fetcher directly (e.g. a full rebuild — not exposed through the CLI):
uv run python eodhd/fetch_eodhd_us_fundamentals.py --update
uv run python eodhd/fetch_eodhd_us_fundamentals.py --full-refresh
```

See `FUNDAMENTALS_REFRESH_DESIGN.md` for the rationale and details.

## 9. News lane

One global crawl per UTC day of the `/news` feed (all symbols), deduplicated on a
hash of the link, stored as one zstd parquet per day under `news/articles/`, with a
day-keyed `news_fetch_state.csv`. See `EODHD_NEWS_SENTIMENT_FINDINGS.md` for the
measured feed facts, the design, and the backfill result.

```powershell
uv run python eodhd/fetch_eodhd_news.py                        # backfill / resume (uncapped; newest days first)
uv run python eodhd/fetch_eodhd_news.py --from 2026-08-01      # bounded window
uv run python eodhd/cli.py refresh news --run                  # top-up only (capped at 30 days by the registry)
uv run python eodhd/cli.py status news
```

`refresh` (both paths) includes the news top-up by default; the registry pins
`--limit-days 30` so a routine refresh can never become a backfill. Ticker-style
flags (`--tickers`, `--to`, `--full-refresh`) never reach the crawler.

## 10. Typical run order (per-ticker scripts)

Refresh universes first, then prices, then event histories. `refresh <lane> --run`
does exactly this; the scripts are listed for windowed / targeted use.

### US / UK-EU common stock

```powershell
uv run python eodhd/fetch_eodhd_us_fundamentals.py           # (initial) writes universe + coverage_summary.csv
uv run python eodhd/fetch_eodhd_us_prices.py
uv run python eodhd/fetch_eodhd_us_dividends.py
uv run python eodhd/fetch_eodhd_us_splits.py

uv run python eodhd/fetch_eodhd_eu_fundamentals.py
uv run python eodhd/fetch_eodhd_eu_prices.py
uv run python eodhd/fetch_eodhd_dividends.py
uv run python eodhd/fetch_eodhd_splits.py
```

### US ETF

```powershell
uv run python eodhd/fetch_eodhd_us_etf_universe.py
uv run python eodhd/fetch_eodhd_us_etf_prices.py --universe provider
uv run python eodhd/fetch_eodhd_us_etf_dividends.py --universe provider
uv run python eodhd/fetch_eodhd_us_etf_splits.py --universe provider
```

Starter sleeve only:

```powershell
uv run python eodhd/fetch_eodhd_us_etf_prices.py --universe starter
uv run python eodhd/fetch_eodhd_us_etf_dividends.py --universe starter
uv run python eodhd/fetch_eodhd_us_etf_splits.py --universe starter
```

### US index / benchmark reference

```powershell
uv run python eodhd/fetch_eodhd_index_ref_universe.py
uv run python eodhd/fetch_eodhd_index_ref_prices.py
```

### UK/EU ETF

```powershell
uv run python eodhd/fetch_eodhd_uk_eu_etf_universe.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_prices.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_dividends.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_splits.py
```

### UK/EU index / benchmark reference

```powershell
uv run python eodhd/fetch_eodhd_uk_eu_index_ref_universe.py
uv run python eodhd/fetch_eodhd_uk_eu_index_ref_prices.py
```

## 11. Useful targeted reruns

Single ticker:

```powershell
uv run python eodhd/fetch_eodhd_uk_eu_etf_prices.py --tickers MAJMEL.CO
uv run python eodhd/fetch_eodhd_uk_eu_etf_dividends.py --tickers MAJMEL.CO
uv run python eodhd/fetch_eodhd_uk_eu_etf_splits.py --tickers MAJMEL.CO
```

Small smoke batch:

```powershell
uv run python eodhd/fetch_eodhd_us_etf_prices.py --universe starter --limit 5 --full-refresh
uv run python eodhd/fetch_eodhd_uk_eu_index_ref_prices.py --limit 5 --full-refresh
```

Windowed rerun:

```powershell
uv run python eodhd/fetch_eodhd_us_etf_prices.py --from 2026-01-01 --to 2026-05-07
uv run python eodhd/fetch_eodhd_uk_eu_etf_dividends.py --from 2025-01-01 --to 2026-05-07
```

## 12. Progress / audit checks

Raw-data quality report. The console output is a colour-coded triage view
(severity glyphs, dataset-kind hues, action-cost colouring); pipe it or pass
`--no-color` for plain text. Scope it by lane, and drill into a single dataset:

```powershell
uv run python eodhd/cli.py qc                    # every price-bearing lane (capped per lane)
uv run python eodhd/cli.py qc us_common          # one lane
uv run python eodhd/cli.py qc us_common splits   # drill-down: all flags for that dataset
uv run python eodhd/cli.py status us_common      # as-of dashboard, one lane

# the underlying script (flags spelled out) still works and can write reports:
uv run python eodhd/report_eodhd_raw_quality.py --lane all --write-report
```

`status`/`qc` take an optional positional `[lane]` (and `qc` a second `[dataset]`
= `prices|dividends|splits`); any `--flags` after them are forwarded untouched. On
an empty root both say so and name the first-fill command
(`refresh <lane> --with-fundamentals --run`, see §2) instead of failing.

The QC report checks for:

- universe/state/output coverage mismatches,
- stale or sparse price histories,
- duplicate or structurally invalid OHLC rows,
- suspicious ETF zero-volume behavior,
- and dividend/split sidecar inconsistencies.

Persistent hard-error symbols that survive targeted `--full-refresh` repairs should be recorded in the relevant lane manifest as known provider-side exceptions rather than repeatedly re-fetched blindly.

When `--write-report` is used, each audited lane root receives:

- `qc_summary.json`
- `qc_flags.csv`

For a lane-specific audit, inspect:

- the main parquet output (`prices_daily.parquet`, `dividends_history.parquet`, `splits_history.parquet`),
- the state sidecar (`*_fetch_state.csv`),
- and for event lanes the audit sidecar (`*_fetch_audit.csv`).

Interpretation rules:

- `ok`: provider returned data and the lane recorded rows.
- `empty`: provider returned no dividend/split history for that pair.
- `up_to_date`: the pair was already covered through the requested upper bound and no HTTP request was needed on this rerun.

## 13. Operator notes

- Keep factual counts and completion status in the manifests, not in this runbook.
- Update the relevant manifest whenever a lane changes scope, output paths, or observed on-disk counts materially.
- If a session is interrupted, rerun the same script(s) without `--full-refresh` unless a rebuild is explicitly intended.
- After any fetch: `reindex` (catalog) and, if you back up, `sync push --run`.
