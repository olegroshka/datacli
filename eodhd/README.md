# EODHD Workflow Runbook

This directory contains the operational fetchers and factual manifests for the `btest`-owned EODHD data lanes.

## Quick start — unified CLI

`cli.py` is the single front door. It is driven by the lane registry in
`eodhd_datasets.py`, so it never hardcodes lane or file names.

> **Interactive shell:** `datacli.py` (repo root) is a cmd2/Rich REPL over all
> data sources. `uv run python datacli.py`, then `/source eodhd` and run
> `/status`, `/fetch --fast --run`, `/qc`, `/config` — it reuses this CLI.

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"

uv run python eodhd/cli.py --help          # all commands
uv run python eodhd/cli.py status          # what data we have, as of when
uv run python eodhd/cli.py status --write   # + regenerate data/raw/eodhd/STATUS.md
uv run python eodhd/cli.py lanes           # registered lanes/datasets/fetchers
uv run python eodhd/cli.py refresh         # show the refresh plan (no fetch)
uv run python eodhd/cli.py refresh --run   # execute: prices + events, all lanes (per-ticker; slow)
uv run python eodhd/cli.py refresh --fast --run   # FAST: bulk endpoints, minutes not hours
uv run python eodhd/cli.py refresh us_common --run
uv run python eodhd/cli.py refresh --with-fundamentals --run
uv run python eodhd/cli.py probe AAPL MSFT NVDA
```

`refresh` is **dry-run by default** and only fetches with `--run` (it hits a paid
API). The individual `fetch_eodhd_*.py` scripts below remain the ground truth and
are still the way to do windowed or otherwise unusual pulls.

## Point the tool at your data, then explore it

The fetchers and the explorer read/write one raw-data root. Resolution order:
`EODHD_DATA_ROOT` env var → `datacli.toml [eodhd].data_root` → `../btest` default.

```powershell
uv run python eodhd/cli.py config                       # show resolved root + source
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
```

`schema` diffs the declared column contract (`schema.py`, versioned) against your
on-disk columns; **projected views** NULL-fill or rename-alias so queries stay
stable as the provider's columns evolve. Re-run `reindex` after any fetch to keep
the catalog current.

## What lives where

- `README.md` (this file): operator-facing entry point for how to run, resume, and audit the EODHD fetchers.
- `cli.py`: unified CLI (`status` / `refresh` / `qc` / `probe` / `lanes`).
- `eodhd_datasets.py`: the lane/dataset registry — single source of truth; add a lane or dataset here.
- `status_eodhd.py`: as-of / staleness reporter; writes `data/raw/eodhd/STATUS.md` + `STATUS.json`.
- `fetch_eodhd_bulk.py`: the fast path — bulk end-of-day refresh (one call per exchange) for prices/dividends/splits; used by `refresh --fast`.
- `fundamentals_refresh_common.py`: shared incremental-refresh helpers for the fundamentals fetchers (target selection, state sidecar, earnings-calendar lookup).
- `FUNDAMENTALS_REFRESH_DESIGN.md`: design notes for the fundamentals refresh (why append-only was a problem, the fix).
- `EODHD_*_MANIFEST.md`: per-lane factual inventories of scope, local artefacts, and current observed counts.
- `fetch_eodhd_*.py`: the actual fetchers.
- `tmp_poll_eodhd_progress.py` at repo root: ad hoc progress snapshot across the ETF and index-reference lanes.

## Current EODHD lane map

### US

- Common-stock lane: `EODHD_US_COMMON_STOCK_DATA_MANIFEST.md`
- ETF lane: `EODHD_US_ETF_DATA_MANIFEST.md`
- Index / benchmark lane: `EODHD_INDEX_REF_DATA_MANIFEST.md`

### UK/EU

- Common-stock lane: `EODHD_UK_EU_DATA_MANIFEST.md`
- ETF lane: `EODHD_UK_EU_ETF_DATA_MANIFEST.md`
- Index / benchmark lane: `EODHD_UK_EU_INDEX_REF_DATA_MANIFEST.md`

## Resume model

The fetchers are designed to be rerun normally.

Continuation state is persisted in the per-lane sidecars:

- prices: `prices_fetch_state.csv`
- dividends: `dividends_fetch_state.csv` plus `dividends_fetch_audit.csv`
- splits: `splits_fetch_state.csv` plus `splits_fetch_audit.csv`

Normal reruns should **not** use `--full-refresh`.

On a normal rerun, the scripts will:

- skip pairs already covered through the requested `--to` bound,
- continue incremental tails for pairs with local history,
- preserve explicit `empty` states for tickers where the provider returned no dividend/split history,
- and merge new output with existing parquet files.

Use `--full-refresh` only when you intentionally want to rebuild an output from scratch.

## Fast refresh (bulk end-of-day)

The per-ticker fetchers make one HTTP call per ticker — tens of thousands of
calls, hours of wall-clock — because they're request-bound (incremental shrinks
the *data* per call, not the *number* of calls). For a routine daily/weekly
top-up, use the bulk path instead:

```powershell
uv run python eodhd/cli.py refresh --fast --run          # all lanes, prices/divs/splits
uv run python eodhd/cli.py refresh --fast us_common      # dry-run, one lane
uv run python eodhd/cli.py refresh --fast --days 3 --run # only fill the last 3 days
```

It pulls a whole exchange's latest day(s) in a single `/eod-bulk-last-day/{EXCHANGE}`
call (shared across lanes on the same exchange), filters to each lane's universe,
and **append-merges** only genuinely-new rows — existing rows are never rewritten,
so multiple dividends on one ex-date are preserved. State advances only to each
pair's actual newest bar (never past real data), so nothing is ever skipped.

Caveats / when to still use the per-ticker path:
- **Adjustments:** bulk appends bars with the provider's current adjustment; it
  does not re-adjust historical `adjusted_close` after a split. Tickers that split
  are reported — run a periodic per-ticker `--full-refresh` for those.
- **Large gaps:** a pair more than `--days` behind is reported and skipped (never
  hole-punched); use the normal `refresh --run` to backfill it.
- **New listings / universe:** `--fast` updates the existing universe; run the
  universe fetchers (or a normal refresh) to discover newly listed tickers.
- **Fundamentals:** not part of `--fast`; use `refresh --datasets fundamentals --run`.

## Fundamentals refresh

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
correctly.

```powershell
# Routine incremental refresh (calendar-targeted) via the CLI:
uv run python eodhd/cli.py refresh --datasets fundamentals --run

# Or a fetcher directly (e.g. a full rebuild — not exposed through the CLI):
uv run python eodhd/fetch_eodhd_us_fundamentals.py --update
uv run python eodhd/fetch_eodhd_us_fundamentals.py --full-refresh
```

See `FUNDAMENTALS_REFRESH_DESIGN.md` for the rationale and details.

## Typical run order

Refresh universes first, then prices, then event histories.

### US ETF

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"
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
Set-Location "C:\Users\olegr\PycharmProjects\btest"
uv run python eodhd/fetch_eodhd_index_ref_universe.py
uv run python eodhd/fetch_eodhd_index_ref_prices.py
```

### UK/EU ETF

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"
uv run python eodhd/fetch_eodhd_uk_eu_etf_universe.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_prices.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_dividends.py
uv run python eodhd/fetch_eodhd_uk_eu_etf_splits.py
```

### UK/EU index / benchmark reference

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"
uv run python eodhd/fetch_eodhd_uk_eu_index_ref_universe.py
uv run python eodhd/fetch_eodhd_uk_eu_index_ref_prices.py
```

## Useful targeted reruns

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

## Progress / audit checks

Quick cross-lane snapshot:

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"
python -u tmp_poll_eodhd_progress.py
```

Raw-data quality report:

```powershell
Set-Location "C:\Users\olegr\PycharmProjects\btest"
uv run python eodhd/report_eodhd_raw_quality.py --lane all

# write machine-readable per-lane outputs next to the raw data
uv run python eodhd/report_eodhd_raw_quality.py --lane all --write-report
```

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

## Operator notes

- Keep factual counts and completion status in the manifests, not in this runbook.
- Update the relevant manifest whenever a lane changes scope, output paths, or observed on-disk counts materially.
- If a session is interrupted, rerun the same script(s) without `--full-refresh` unless a rebuild is explicitly intended.

