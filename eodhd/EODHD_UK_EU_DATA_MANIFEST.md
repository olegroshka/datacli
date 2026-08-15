# EODHD UK/EU Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / RESUME-READY  
**Created:** 2026-05-05  
**Purpose:** btest-owned operational inventory for the UK/EU EODHD workflow  
**Source provenance:** adapted from HARP EODHD fetch/docs planning; not a live mirror of `harp`

**Live counts:** the numbers below were observed on the date shown and are not auto-updated; for current on-disk counts run `uv run python eodhd/cli.py status <lane>` or read `<data-root>/STATUS.md` (written by `status --write`).

## 1. Scope

This file is the `btest`-local inventory for the UK/EU EODHD workflow.

It exists so that:

- `harp` can remain untouched,
- the migration can proceed in `btest` without losing provenance,
- and the expanded acquisition plan (dividends, splits, snapshots, raw cache) has a btest-owned home.

## 2. Important status note

At creation time, this was a **planning manifest**, not an execution manifest.

That originally meant:

- paths listed here are the intended `btest` destinations,
- datasets described here are target artefacts,
- row counts and coverage numbers from `harp` are **not** yet claimed as true in `btest`,
- and the file should only be upgraded to a factual inventory as the migrated scripts are run inside `btest`.

As of `2026-05-06`, the core UK/EU EODHD artefacts below are now factual `btest` on-disk observations, including their current continuation sidecars.

### Current workflow status (2026-05-06)

Completed in `btest`:

- `eodhd/fetch_eodhd_eu_fundamentals.py`
- `eodhd/fetch_eodhd_eu_prices.py`
- `eodhd/fetch_eodhd_macro.py`
- `eodhd/probe_eodhd_fundamentals_schema.py`
- private raw payload caching inside `fetch_eodhd_eu_fundamentals.py`
- live 6-ticker schema probe report at `data/raw/eodhd/uk_eu/probe_schema_report.json`
- first-pass same-call extractors for `SplitsDividends`, `Earnings`, `SharesStats`, `outstandingShares`, `Highlights`, and `Valuation`
- first end-to-end `btest` materialization run for 6 pilot tickers using cached raw payloads only
- dedicated event-history fetcher scripts implemented: `fetch_eodhd_dividends.py`, `fetch_eodhd_splits.py`
- broad qualifying-universe price pull completed for all `1,735 / 1,735` qualifying pairs
- broad dedicated dividend-history pull completed
- broad dedicated split-history pull completed

Still optional / not yet completed in `btest`:

- any optional provider-registry integration work.

## 3. Local paths in `btest`

### 3.1 Raw / derived local outputs

Primary landing zone:

- `data/raw/eodhd/uk_eu/tickers_*.parquet`
- `data/raw/eodhd/uk_eu/fundamentals_quarterly.parquet`
- `data/raw/eodhd/uk_eu/firm_metadata.parquet`
- `data/raw/eodhd/uk_eu/coverage_summary.csv`
- `data/raw/eodhd/uk_eu/prices_daily.parquet`
- `data/raw/eodhd/uk_eu/macro_eu.parquet`
- `data/raw/eodhd/uk_eu/macro_coverage.csv`

Current on-disk after the 6-ticker pilot materialization:

- `data/raw/eodhd/uk_eu/fundamentals_quarterly.parquet`
- `data/raw/eodhd/uk_eu/firm_metadata.parquet`
- `data/raw/eodhd/uk_eu/coverage_summary.csv`
- `data/raw/eodhd/uk_eu/splits_dividends_snapshot.parquet`
- `data/raw/eodhd/uk_eu/dividend_counts_by_year.parquet`
- `data/raw/eodhd/uk_eu/shares_stats_snapshot.parquet`
- `data/raw/eodhd/uk_eu/highlights_snapshot.parquet`
- `data/raw/eodhd/uk_eu/valuation_snapshot.parquet`
- `data/raw/eodhd/uk_eu/outstanding_shares_annual.parquet`
- `data/raw/eodhd/uk_eu/outstanding_shares_quarterly.parquet`
- `data/raw/eodhd/uk_eu/earnings_history.parquet`
- `data/raw/eodhd/uk_eu/earnings_trend.parquet`
- `data/raw/eodhd/uk_eu/earnings_annual.parquet`
- `data/raw/eodhd/uk_eu/probe_schema_report.json`

Current on-disk after the 20-name smoke basket:

- `fundamentals_quarterly.parquet` — 6,676 rows, 20 firms
- `firm_metadata.parquet` — 20 rows, 20 firms
- `coverage_summary.csv` — 20 rows, 20 firms
- `splits_dividends_snapshot.parquet` — 20 rows, 20 firms
- `dividend_counts_by_year.parquet` — 676 rows, 20 firms
- `shares_stats_snapshot.parquet` — 20 rows, 20 firms
- `highlights_snapshot.parquet` — 20 rows, 20 firms
- `valuation_snapshot.parquet` — 20 rows, 20 firms
- `outstanding_shares_annual.parquet` — 592 rows, 20 firms
- `outstanding_shares_quarterly.parquet` — 2,274 rows, 20 firms
- `earnings_history.parquet` — 1,615 rows, 20 firms
- `earnings_trend.parquet` — 536 rows, 20 firms
- `earnings_annual.parquet` — 425 rows, 20 firms

### 3.2 Planned expansion outputs

First-pass same-call outputs now implemented in the fetcher:

- `data/raw/eodhd/uk_eu/splits_dividends_snapshot.parquet`
- `data/raw/eodhd/uk_eu/dividend_counts_by_year.parquet`
- `data/raw/eodhd/uk_eu/shares_stats_snapshot.parquet`
- `data/raw/eodhd/uk_eu/highlights_snapshot.parquet`
- `data/raw/eodhd/uk_eu/valuation_snapshot.parquet`
- `data/raw/eodhd/uk_eu/outstanding_shares_annual.parquet`
- `data/raw/eodhd/uk_eu/outstanding_shares_quarterly.parquet`
- `data/raw/eodhd/uk_eu/earnings_history.parquet`
- `data/raw/eodhd/uk_eu/earnings_trend.parquet`
- `data/raw/eodhd/uk_eu/earnings_annual.parquet`

Still-planned / unresolved outputs if a later endpoint is needed:

- `data/raw/eodhd/uk_eu/earnings_events.parquet`
- `data/raw/eodhd/uk_eu/fundamentals_fetch_audit.csv`

Continuation / audit sidecars now materialized:

- `data/raw/eodhd/uk_eu/prices_fetch_state.csv`
- `data/raw/eodhd/uk_eu/dividends_fetch_audit.csv`
- `data/raw/eodhd/uk_eu/dividends_fetch_state.csv`
- `data/raw/eodhd/uk_eu/splits_fetch_audit.csv`
- `data/raw/eodhd/uk_eu/splits_fetch_state.csv`

Dedicated endpoint fetchers now implemented for the two event-history outputs above:

- `eodhd/fetch_eodhd_dividends.py`
- `eodhd/fetch_eodhd_splits.py`

These scripts are implemented, tested, and now executed on the full `both_60q == 1` qualifying universe.

Operational refinement added after the broad runs:

- both event-history fetchers now flush persisted output/audit state based on **all attempted API calls**, not only `ok` / `empty` outcomes,
- so long stretches of provider empties / errors no longer make on-disk resume state appear frozen.

### 3.3 Private cache area

Private, local-only cache:

- `data/raw/eodhd/uk_eu/cache/fundamentals/{exchange}/{ticker}.json.gz`

This cache is not for git and not for public redistribution.

### 3.4 Readiness gate before broad downloads

Before any qualifying-universe or full-universe UK/EU fundamentals pull, the following must be true:

1. the schema probe has been run on a small pilot basket,
2. the top-level payload sections and key same-call subsections have been reviewed,
3. raw payload cache files are being written successfully,
4. the first-pass extraction scope is frozen so we do not spend calls before deciding what to retain.

Until those conditions are met, only tiny probe / smoke runs should be used.

### 3.5 Confirmed schema-probe findings (pilot run: 2026-05-05)

Live pilot basket used:

- `SHEL.LSE`
- `BP.LSE`
- `AZN.LSE`
- `HSBA.LSE`
- `GSK.LSE`
- `ULVR.LSE`

Confirmed top-level sections present in the current fundamentals payload:

- `ESGScores`
- `Earnings`
- `Financials`
- `General`
- `Highlights`
- `Holders`
- `InsiderTransactions`
- `SharesStats`
- `SplitsDividends`
- `Technicals`
- `Valuation`
- `outstandingShares`

Confirmed `Financials` subsections:

- `Balance_Sheet`
- `Cash_Flow`
- `Income_Statement`

Immediate implication for call-efficiency:

- dividend / split summary enrichment should target `SplitsDividends`,
- earnings-event enrichment should target `Earnings`,
- capital-structure enrichment should target `SharesStats` and `outstandingShares`,
- snapshot enrichment should target `Highlights` and `Valuation`,
- and all of those should be extracted from the same cached fundamentals payload before considering extra endpoints.

Observed limitation from the live pilot:

- `SplitsDividends` gave summary fields plus `NumberDividendsByYear`,
- but it did **not** provide true event-level dividend or split records in the inspected payload,
- so `dividends_history.parquet` / `splits_history.parquet` remain unresolved and should not be assumed available from the same call.

### 3.6 First pilot materialization run (2026-05-05)

Executed targeted pull:

- `SHEL.LSE`
- `BP.LSE`
- `AZN.LSE`
- `HSBA.LSE`
- `GSK.LSE`
- `ULVR.LSE`

Observed run summary:

- firms materialized: `6`
- payloads fetched from API: `0`
- payloads reused from cache: `6`
- firms skipped: `0`
- firms with both >=56Q (2011-2025): `6`

This confirms that the current `btest` workflow can perform end-to-end materialization from the private raw cache without re-spending fundamentals calls for already-probed names.

### 3.7 Full smoke-basket run (2026-05-05)

Executed:

- `uv run python eodhd/fetch_eodhd_eu_fundamentals.py --smoke`

Observed run summary:

- firms materialized: `20`
- payloads fetched from API: `14`
- payloads reused from cache: `6`
- firms skipped: `0`
- firms with both >=56Q (2011-2025): `16`

Exchange mix in the smoke basket coverage summary:

- `LSE`: 8 firms, 8 with >=56Q both
- `XETRA`: 6 firms, 5 with >=56Q both
- `PA`: 4 firms, 2 with >=56Q both
- `SW`: 2 firms, 1 with >=56Q both

This smoke run confirms that the migrated `btest` workflow can:

- mix already-cached pilot payloads with newly fetched names,
- write all first-pass derived outputs successfully,
- and maintain the intended call-efficiency profile (`6` cache reuses, `14` new payload fetches) on a larger validation batch.

### 3.8 Broad default-exchange pull completed (2026-05-06)

Completion checkpoint observed at `2026-05-06T00:26:05`.

Final on-disk scale:

- raw payload cache files: `7,611`
- `firm_metadata.parquet`: `7,611` rows / firms
- `fundamentals_quarterly.parquet`: `1,197,191` rows across `7,186` firms
- `coverage_summary.csv`: `7,186` rows / firms
- observed quarterly statement date range in `fundamentals_quarterly.parquet`: `1984-12-31` -> `2026-04-04`
- coverage-summary first/last ranges:
  - `bs_first`: `1984-12-31` -> `2025-12-31`
  - `bs_last`: `2001-03-31` -> `2026-04-04`
  - `cf_first`: `1986-09-30` -> `2025-12-31`
  - `cf_last`: `2000-06-30` -> `2026-04-04`

Interpretation:

- the broad default-exchange fundamentals pull is complete,
- the private raw payload cache now covers the full current exchange-listed working universe,
- and the broad same-call derived outputs can now be backfilled or refreshed offline from cached payloads without re-spending fundamentals calls for those `7,611` names.

What remains outside this completed scope:

- event-level dividend / split history is still unresolved from the inspected fundamentals payload,
- price pulls are a separate stage,
- provider-registry / platform integration is still optional future work.

### 3.9 Broad-pull audit findings (2026-05-06)

Audit facts from the completed broad fundamentals stage:

- `coverage_summary.csv` rows / firms: `7,186`
- `both_60q` total: `1,735`
- firms with any assets data in 2011-2025: `6,859`
- firms with any capex data in 2011-2025: `7,030`
- cached raw payload files: `7,611`
- duplicate fundamentals rows on (`ticker`, `exchange`, `statement`, `date`): `0`

Exchange-level `both_60q` audit summary:

| Exchange | Firms | >=56Q both |
|---|---:|---:|
| AS | 108 | 22 |
| CO | 147 | 86 |
| HE | 191 | 82 |
| LSE | 3,863 | 760 |
| MC | 218 | 35 |
| OL | 290 | 98 |
| PA | 585 | 31 |
| ST | 794 | 261 |
| SW | 219 | 24 |
| VI | 74 | 6 |
| XETRA | 697 | 330 |

### 3.10 Event-level dividend / split investigation (2026-05-06)

Findings:

- the inspected `SplitsDividends` section inside the fundamentals payload still exposes only summary fields plus `NumberDividendsByYear`,
- the daily `eod/{ticker}.{exchange}` endpoint returns OHLCV + `adjusted_close`, but no explicit dividend/split event columns in the tested samples,
- dedicated event-history endpoints were confirmed live:
  - `div/{ticker}.{exchange}` -> event-level dividend rows
  - `splits/{ticker}.{exchange}` -> event-level split rows

Implication:

- true event-level dividend / split history should be treated as a **separate endpoint stage**,
- not as something already recoverable from the cached fundamentals payload or current price endpoint.

Implementation status after the investigation:

- the separate endpoint stage is now coded in `btest`,
- `dividends_history.parquet` and `splits_history.parquet` are the intended outputs,
- fetch-audit sidecars are also implemented (`dividends_fetch_audit.csv`, `splits_fetch_audit.csv`) so empty-history names do not waste repeated calls,
- the broad event-history runs are now complete for the current qualifying universe,
- and the paired state sidecars (`dividends_fetch_state.csv`, `splits_fetch_state.csv`) are now current through `2026-05-06` for all `1,735` qualifying pairs.

### 3.11 Price pull stage status (completed 2026-05-06; current through query upper bound `2026-05-06`)

The qualifying-universe daily price pull was started via:

- `uv run python eodhd/fetch_eodhd_eu_prices.py`

Selection rule for this stage:

- use firms with `both_60q == 1` from `coverage_summary.csv`
- current qualifying-universe size: `1,735` firms

Current on-disk result:

- `prices_daily.parquet`: `6,598,920` rows
- qualifying pairs with price history present: `1,735`
- qualifying pairs expected from `coverage_summary.csv`: `1,735`
- date range: `2005-01-02` -> `2026-05-05`
- `prices_fetch_state.csv`: `1,735` rows, all currently `up_to_date`
- `coverage_through` in the state sidecar: `2026-05-06` for every qualifying pair
- `next_resume_from` currently ranges from `2026-03-15` to `2026-04-30`, depending on each pair's last observed local price date and the configured overlap

Operational update after the later incremental catch-up:

- the formerly missing qualifying pair `0L2T.LSE` is now present locally,
- so there is no remaining qualifying-universe price gap on disk as of this checkpoint.

Interpretation:

- the price stage is operationally complete,
- the latest requested query ceiling is `2026-05-06`,
- and the observed max stored trading date is `2026-05-05`, which is the latest provider/trading-day value returned in the current refresh.

Operational note after the stale-end-date correction:

- the original port had a hard-coded upper bound of `2025-12-31` and treated cached tickers as permanently complete,
- `fetch_eodhd_eu_prices.py` now supports **incremental tail refreshes** up to the current date,
- so future catch-up runs no longer need to re-download full history payloads for already-cached names.

### 3.12 Dividend-history stage status (completed 2026-05-06)

Executed via:

- `uv run python eodhd/fetch_eodhd_dividends.py`

Final on-disk result:

- `dividends_history.parquet`: `48,202` rows across `1,487` firms
- `dividends_fetch_audit.csv`: `1,735` audited qualifying pairs
- audit outcome:
  - `1,487` = `ok`
  - `248` = `empty`
- event-date range: `1972-07-29` -> `2027-03-30`
- `dividends_fetch_state.csv`: `1,735` rows, all currently `up_to_date`
- state `coverage_through`: `2026-05-06` for every qualifying pair
- `next_resume_from` currently ranges from `1998-04-12` to `2026-05-01`

Interpretation:

- the dedicated dividend endpoint stage is complete for the current qualifying universe,
- and reruns can now avoid re-spending calls on both successful and empty-history names.

### 3.13 Split-history stage status (completed 2026-05-06)

Executed via:

- `uv run python eodhd/fetch_eodhd_splits.py`

Final on-disk result:

- `splits_history.parquet`: `2,145` rows across `933` firms
- `splits_fetch_audit.csv`: `1,735` audited qualifying pairs
- audit outcome:
  - `933` = `ok`
  - `802` = `empty`
- event-date range: `1982-08-02` -> `2026-04-07`
- `splits_fetch_state.csv`: `1,735` rows, all currently `up_to_date`
- state `coverage_through`: `2026-05-06` for every qualifying pair
- `next_resume_from` currently ranges from `1988-07-22` to `2026-05-01`

Interpretation:

- the dedicated split endpoint stage is complete for the current qualifying universe,
- and empty-history names are now explicitly recorded so future reruns stay low-waste.

### 3.14 Incremental continuation semantics (current working rule)

The continuation SSOT is now the per-pair state sidecar for each endpoint family:

- prices: `data/raw/eodhd/uk_eu/prices_fetch_state.csv`
- dividends: `data/raw/eodhd/uk_eu/dividends_fetch_state.csv`
- splits: `data/raw/eodhd/uk_eu/splits_fetch_state.csv`

How the next run continues:

- **Prices**: `fetch_eodhd_eu_prices.py` uses `latest_data_date` when local history exists; otherwise it falls back to `coverage_through` for already-checked empty-history pairs. It then computes a per-pair tail window with the configured overlap (`--overlap-days`, default `5`) and stores the next recommended lower bound in `next_resume_from`.
- **Dividends / splits**: `fetch_eodhd_dividends.py` and `fetch_eodhd_splits.py` use the same state-driven rule via `choose_incremental_window(...)`: pairs with local event history resume from the last local event date minus overlap, while empty-history pairs resume from `coverage_through` minus overlap instead of re-querying full history.
- When a run makes no HTTP request because a pair is already covered through the current `--to`, the state row is recorded as `up_to_date`. In that case `query_from` is intentionally blank; the continuation anchor to trust is `next_resume_from` plus `coverage_through`.

Operational consequence for the next session:

- a normal rerun of the three scripts is now sufficient to continue incrementally;
- no manual ticker bookkeeping is required;
- and future catch-up calls should be limited to genuinely uncovered tails unless `--full-refresh` is explicitly requested.

## 4. Planned script home in `btest`

The migrated scripts are expected to live under:

- `eodhd/fetch_eodhd_eu_fundamentals.py`
- `eodhd/fetch_eodhd_eu_prices.py`
- `eodhd/fetch_eodhd_macro.py`
- `eodhd/probe_eodhd_fundamentals_schema.py`
- `eodhd/report_eodhd_coverage.py` *(optional later)*

## 5. Data families the migrated workflow should support

### A. Current core data families

These are the first expected migrated outputs:

1. ticker lists by exchange
2. quarterly fundamentals (BS / CF / IS)
3. firm metadata
4. coverage summary
5. daily prices for qualifying firms
6. macro series used in the UK/EU workflow

### B. Expansion data families

These are the additional planned families that motivated the move out of `harp`:

1. dividend history
2. split history
3. richer static company metadata
4. shares / capital-structure snapshots
5. curated highlights / valuation snapshots
6. earnings / reporting event metadata

## 6. Migration principles carried over from the HARP planning work

1. Extract as much value as possible from the existing `fundamentals/{ticker}.{exchange}` call.
2. Add separate endpoints only when a real research need remains after same-call extraction is exhausted.
3. Prefer private raw payload caching so new schema extraction can happen offline.
4. Keep research convenience outputs in `btest`; keep paper-production outputs in `harp` untouched.
5. Do not copy raw vendor data from `harp`.
6. Treat `both_60q` as a legacy field name that currently means "min(assets, capex) >= 56 quarters in 2011-2025", not 60 literal quarters.

## 7. Maintenance rule

Update this manifest when the `eodhd/` workflow changes materially, especially when any of the following move:

- fetch dates,
- exchange coverage,
- schema or sidecar semantics,
- row counts,
- coverage stats,
- confirmed payload sections present in the EODHD fundamentals response,
- or actual call counts spent by the btest-owned workflow.

## 8. Operational ownership

This manifest remains the factual inventory for the core UK/EU common-stock lane.

Operator-facing entry-point documentation for calling and resuming the broader EODHD fetchers now lives in `eodhd/README.md`.

The lane-specific manifests under `eodhd/` remain the source for per-sleeve scope, output inventory, continuation semantics, and observed on-disk counts.


