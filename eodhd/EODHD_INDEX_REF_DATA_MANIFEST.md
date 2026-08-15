# EODHD Index / Benchmark Reference Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / FULL-PRICE-RUN-COMPLETE  
**Created:** 2026-05-06  
**Purpose:** btest-owned operational inventory for the EODHD index / benchmark reference workflow  
**Source provenance:** separate reference sleeve alongside the completed US common-stock and expanding ETF lanes in `eodhd/`

**Live counts:** the numbers below were observed on the date shown and are not auto-updated; for current on-disk counts run `uv run python eodhd/cli.py status <lane>` or read `<data-root>/STATUS.md` (written by `status --write`).

## 1. Scope

This file is the `btest`-local execution note for the index / benchmark reference lane under EODHD.

This sleeve is intentionally separate from the issuer and ETF universes because it uses the provider's dedicated `INDX` exchange for reference indices / benchmarks rather than tradable securities.

Current target universe:

- the EODHD `INDX` exchange master list,
- kept as a direct provider-maintained reference universe,
- with daily price history fetched from `eod/{ticker}.INDX`,
- and no dedicated dividend / split history expected for the direct index sleeve.

## 2. Planned local paths in `btest`

Primary landing zone:

- `data/raw/eodhd/index_ref/tickers_INDX.parquet`
- `data/raw/eodhd/index_ref/prices_daily.parquet`
- `data/raw/eodhd/index_ref/prices_fetch_state.csv`

## 3. Current observed scope and completion state

### Current observed live scope (2026-05-07)

Observed live counts from the EODHD `exchange-symbol-list/INDX` endpoint:

- provider index / benchmark list rows: `1,666`
- provider `Type` observed: `INDEX`

Observed direct benchmark/index probes against `eod/{ticker}.INDX`:

- `GSPC.INDX`: returned recent daily history successfully
- `NDX.INDX`: returned recent daily history successfully
- `IXIC.INDX`: returned recent daily history successfully
- `DJI.INDX`: returned recent daily history successfully
- `VIX.INDX`: returned recent daily history successfully

This confirms the direct reference-index lane is viable without mapping everything through ETF proxies.

### Phase 0 — bootstrap

Deliverables:

- `eodhd/fetch_eodhd_index_ref_universe.py`
- `eodhd/fetch_eodhd_index_ref_prices.py`
- this manifest
- targeted unit tests for provider-universe target loading
- a small live benchmark smoke pull, then a full-provider run

Completed live bootstrap on `2026-05-06`:

- `tickers_INDX.parquet` written successfully from the provider `INDX` exchange list
- focused benchmark smoke pull completed successfully on `GSPC.INDX`, `NDX.INDX`, `IXIC.INDX`, `DJI.INDX`, and `VIX.INDX`

Observed bootstrap results:

- `tickers_INDX.parquet`: `1,666` provider index / benchmark rows
- top observed countries in the provider list:
  - `USA`: `906`
  - `Unknown`: `318`
  - `India`: `91`
  - `France`: `43`
  - `UK`: `39`
- smoke `prices_daily.parquet`: `27,732` rows across `5` benchmark symbols
- smoke price date range: `2005-01-01` -> `2026-05-06`
- smoke `prices_fetch_state.csv`: `5` rows, all `ok`

### Completed full-provider price pull

Completed on-disk checkpoint after the full-provider run:

- `tickers_INDX.parquet`: `1,666` provider index / benchmark rows
- `prices_daily.parquet`: `6,456,486` rows across `1,666 / 1,666` benchmark symbols
- observed price date range: `2005-01-01` -> `2026-05-06`
- `prices_fetch_state.csv`: `1,666` rows
  - `1,661` = `ok`
  - `5` = `up_to_date`

Interpretation:

- the direct `INDX` provider universe is fully materialized locally for daily price history,
- the smoke benchmark set remains useful for tiny validation pulls,
- and normal reruns can continue incrementally from `prices_fetch_state.csv` without rebuilding the whole sleeve.

### Current sidecar semantics

- `prices_fetch_state.csv`: `ok` means the run fetched and merged index history for that symbol; `up_to_date` means local history was already current through the requested `--to` bound and no HTTP request was needed.

### QC remediation note (2026-05-07)

After the initial QC report surfaced structural price anomalies, the following symbols were re-fetched with targeted `--tickers ... --full-refresh` repairs:

- `COR10D.INDX`
- `COR90D.INDX`
- `SCXP.INDX`
- `SX3R.INDX`
- `SX6R.INDX`
- `SXER.INDX`

Observed result after the targeted repairs and a second QC pass:

- the same structural anomalies remained present,
- and additional persistent structural exceptions are now treated as known provider-side oddities rather than local resume / merge corruption.

Current known persistent index-reference price exceptions:

- `COR10D.INDX` -> `invalid_ohlc_relationship`
- `COR90D.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SCXP.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SX3R.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SX6R.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXER.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXIR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXKR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXOOR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXQR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXRR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `SXTR.INDX` -> `invalid_ohlc_relationship`, `non_positive_prices`

Operator rule:

- do **not** keep re-running routine targeted full-refresh repairs for these exact symbols,
- continue normal incremental lane reruns for the broader `INDX` universe,
- and only revisit the exception list after provider-side changes are observed or after introducing explicit sanitation / exclusion rules downstream.

## 4. Maintenance rule

Update this manifest when the index / benchmark workflow changes materially, especially when any of the following move:

- provider universe definition,
- output paths,
- row counts,
- state sidecar semantics,
- or benchmark/index scope.

