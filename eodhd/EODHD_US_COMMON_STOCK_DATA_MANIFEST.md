# EODHD US Common-Stock Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / PRICES-AND-EVENTS-COMPLETE  
**Created:** 2026-05-06  
**Purpose:** btest-owned operational inventory for the US common-stock EODHD workflow  
**Source provenance:** adapted from the completed UK/EU EODHD workflow in `eodhd/`

## 1. Scope

This file is the `btest`-local execution note for the US common-stock lane under EODHD.

For this first slice, the target universe is:

- the EODHD `US` exchange master list,
- filtered to `Type == "Common Stock"`,
- then narrowed to the primary listing venues `NASDAQ`, `NYSE`, `NYSE MKT`, and `AMEX`,
- then cleaned further to exclude obvious misclassified wrappers / depositary listings (rights, units, warrants, ADR / depositary-share style names),
- intentionally excluding ETFs, ETNs, preferreds, warrants, rights, units, and other wrappers.

Those non-common instrument families should be added later as separate sleeves (`etf`, `index_ref`, `special_wrappers`) rather than mixed into the issuer master.

## 2. Planned local paths in `btest`

Primary landing zone:

- `data/raw/eodhd/us_common/tickers_US.parquet`
- `data/raw/eodhd/us_common/fundamentals_quarterly.parquet`
- `data/raw/eodhd/us_common/firm_metadata.parquet`
- `data/raw/eodhd/us_common/coverage_summary.csv`
- `data/raw/eodhd/us_common/splits_dividends_snapshot.parquet`
- `data/raw/eodhd/us_common/dividend_counts_by_year.parquet`
- `data/raw/eodhd/us_common/shares_stats_snapshot.parquet`
- `data/raw/eodhd/us_common/highlights_snapshot.parquet`
- `data/raw/eodhd/us_common/valuation_snapshot.parquet`
- `data/raw/eodhd/us_common/outstanding_shares_annual.parquet`
- `data/raw/eodhd/us_common/outstanding_shares_quarterly.parquet`
- `data/raw/eodhd/us_common/earnings_history.parquet`
- `data/raw/eodhd/us_common/earnings_trend.parquet`
- `data/raw/eodhd/us_common/earnings_annual.parquet`

Private raw cache:

- `data/raw/eodhd/us_common/cache/fundamentals/US/{ticker}.json.gz`

## 3. Initial execution plan

### Current observed live scope (2026-05-06)

Observed live counts from the EODHD `exchange-symbol-list/US` endpoint:

- raw `Type == "Common Stock"` rows from the provider US master list: `18,748`
- after the primary-exchange venue filter: `6,661`
- after wrapper / ADR / depositary cleanup: `6,123`

Current venue mix in the cleaned raw US common-stock universe:

- `NASDAQ`: `3,663`
- `NYSE`: `2,193`
- `NYSE MKT`: `246`
- `AMEX`: `21`

### Phase 0 — bootstrap

Deliverables:

- `eodhd/fetch_eodhd_us_fundamentals.py`
- this manifest
- targeted unit tests for the US bootstrap helper behavior
- a small explicit US ticker bootstrap run to confirm paths, cache writes, and parquet outputs

Completed on `2026-05-06`:

- explicit bootstrap run on `AAPL.US`, `MSFT.US`, `JPM.US`, `XOM.US`, `WMT.US`
- outputs materialized under `data/raw/eodhd/us_common/`
- bootstrap result: `2,360` quarterly statement rows across `5` firms
- bootstrap coverage result: `5 / 5` firms with `both_60q == 1`
- raw cache files written: `5`

### Phase 1 — smoke and pilot

Execute in order:

1. `--tickers` bootstrap on a few known liquid names
2. `--smoke` on a 20-name basket
3. `--limit 100` or similar pilot on the US master list

Current pilot checkpoint on `2026-05-06`:

- first cleaned master-list pilot executed via `--limit 25`
- `tickers_US.parquet` written from the cleaned live US master list
- cumulative on-disk fundamentals after the pilot: `7,201` quarterly statement rows across `27` firms
- cumulative metadata rows: `28`
- cumulative `coverage_summary.csv` rows: `27`
- cumulative `both_60q == 1`: `17`

Exit criteria:

- ticker list fetch works for `exchange-symbol-list/US`
- `Type == "Common Stock"` filtering behaves as expected
- raw payload cache writes cleanly under `data/raw/eodhd/us_common/cache/`
- `fundamentals_quarterly.parquet`, `firm_metadata.parquet`, and `coverage_summary.csv` are written successfully

### Phase 2 — full current-listed US common-stock pull

Executed successfully on `2026-05-06`.

Observed final on-disk scale:

- cleaned raw US common-stock universe: `6,123` names
- raw payload cache files: `6,123`
- `firm_metadata.parquet`: `6,123` rows / firms
- `fundamentals_quarterly.parquet`: `1,201,905` rows across `5,859` firms
- `coverage_summary.csv`: `5,859` rows / firms
- observed quarterly statement date range in `fundamentals_quarterly.parquet`: `1983-06-30` -> `2026-03-31`
- coverage-summary first/last ranges:
  - `bs_first`: `1983-06-30` -> `2026-03-31`
  - `bs_last`: up to `2026-03-31`
  - `cf_first`: `1985-06-30` -> `2026-03-31`
  - `cf_last`: up to `2026-03-31`
- firms with any assets data in 2011-2025: `5,839`
- firms with any capex data in 2011-2025: `5,775`
- firms with `both_60q == 1`: `2,580`

Observed same-call derived outputs after the full run:

- `splits_dividends_snapshot.parquet`: `6,123` rows / firms
- `dividend_counts_by_year.parquet`: `50,853` rows across `2,773` firms
- `shares_stats_snapshot.parquet`: `6,123` rows / firms
- `highlights_snapshot.parquet`: `6,123` rows / firms
- `valuation_snapshot.parquet`: `6,123` rows / firms
- `outstanding_shares_annual.parquet`: `107,060` rows across `5,849` firms
- `outstanding_shares_quarterly.parquet`: `405,631` rows across `5,849` firms
- `earnings_history.parquet`: `332,419` rows across `5,649` firms
- `earnings_trend.parquet`: `128,434` rows across `5,046` firms
- `earnings_annual.parquet`: `85,986` rows across `5,648` firms

Interpretation:

- the US raw common-stock fundamentals stage is complete,
- the cleaned live issuer universe is materially in line with the earlier planning range,
- and the qualifying subset for downstream prices / events is now fixed at `2,580` firms.

### Phase 3 — qualifying US price pull

Executed successfully on `2026-05-06`.

Observed final on-disk scale:

- target qualifying subset from `coverage_summary.csv`: `2,580` firms
- `prices_daily.parquet`: `12,518,199` rows across `2,580` firms
- observed price date range: `2005-01-03` -> `2026-05-05`
- `prices_fetch_state.csv`: `2,580` rows, all covered through the current query ceiling

Observed run summary:

- attempted API pulls: `2,575`
- fetched price histories: `2,575`
- skipped / already-covered or empty cases: `5`

Interpretation:

- the full qualifying US price stage is complete,
- the price sidecar semantics are now in place for incremental tail refreshes,
- and the US common-stock lane now has a complete price panel for the qualifying subset.

### Phase 4 — dedicated US dividend / split event-history pulls

Executed successfully on `2026-05-06`.

Dividend-stage result:

- `dividends_history.parquet`: `164,166` rows across `1,889` firms
- observed dividend event-date range: `1970-01-19` -> `2026-05-06`
- `dividends_fetch_audit.csv`: `2,580` audited qualifying pairs
  - `1,889` = `ok`
  - `691` = `empty`
- `dividends_fetch_state.csv`: `2,580` rows
  - `1,884` = `ok`
  - `691` = `empty`
  - `5` = `up_to_date`
- state `coverage_through` max: `2026-05-06`

Split-stage result:

- `splits_history.parquet`: `5,829` rows across `1,880` firms
- observed split event-date range: `1962-10-31` -> `2026-05-04`
- `splits_fetch_audit.csv`: `2,580` audited qualifying pairs
  - `1,880` = `ok`
  - `700` = `empty`
- `splits_fetch_state.csv`: `2,580` rows
  - `1,875` = `ok`
  - `700` = `empty`
  - `5` = `up_to_date`
- state `coverage_through` max: `2026-05-06`

Interpretation:

- the dedicated US event-history stage is complete for the current qualifying subset,
- reruns can now continue incrementally using the same audit/state semantics as UK/EU,
- and the US common-stock lane now has fundamentals, prices, dividends, and splits all materialized locally.

## 4. Follow-on phases (not yet executed)

After the US common-stock fundamentals + prices + events lane is stable:

1. add a separate ETF sleeve
2. add a separate index / benchmark reference sleeve
3. optionally add US event-side manifest detail comparable to the UK/EU document

## 5. Maintenance rule

Update this manifest when the US common-stock workflow changes materially, especially when any of the following move:

- universe definition,
- output paths,
- row counts,
- sidecar semantics,
- smoke / pilot status,
- or call-budget/runtime expectations.

