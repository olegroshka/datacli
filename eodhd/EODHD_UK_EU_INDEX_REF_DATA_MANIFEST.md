# EODHD UK/EU Index / Benchmark Reference Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / FULL-RUN-COMPLETE  
**Created:** 2026-05-06  
**Purpose:** btest-owned operational inventory for the UK/EU index / benchmark EODHD workflow  
**Source provenance:** separate reference sleeve alongside the UK/EU common-stock and ETF lanes in `eodhd/`

**Live counts:** the numbers below were observed on the date shown and are not auto-updated; for current on-disk counts run `uv run python eodhd/cli.py status <lane>` or read `<data-root>/STATUS.md` (written by `status --write`).

## 1. Scope

This file is the `btest`-local execution note for the UK/EU index / benchmark reference lane under EODHD.

Current target universe:

- the provider `INDX` exchange,
- filtered to UK/EU-relevant benchmark/index symbols using regional country tags plus benchmark name/code patterns,
- with direct daily index levels fetched from `eod/{ticker}.INDX`.

## 2. Planned local paths in `btest`

Primary landing zone:

- `data/raw/eodhd/uk_eu_index_ref/tickers_INDX_UK_EU.parquet`
- `data/raw/eodhd/uk_eu_index_ref/prices_daily.parquet`
- `data/raw/eodhd/uk_eu_index_ref/prices_fetch_state.csv`

## 3. Current observed scope and completion state

1. refresh the filtered UK/EU benchmark/index universe from `INDX`
2. run a small explicit benchmark smoke pull
3. run the full-provider UK/EU benchmark/index price job
4. audit final row counts, pair coverage, and date ranges

### Current observed live scope (2026-05-07)

Observed provider UK/EU index / benchmark universe after the first live run:

- provider filtered rows saved to `tickers_INDX_UK_EU.parquet`: `218`
- top observed countries in the filtered list:
  - `France`: `43`
  - `UK`: `39`
  - `Unknown`: `36`
  - `USA`: `24`
  - `Norway`: `19`
  - `Switzerland`: `16`

Completed live smoke checkpoint on `2026-05-06`:

- `fetch_eodhd_uk_eu_index_ref_prices.py --limit 5 --full-refresh`

Observed smoke results:

- `prices_daily.parquet`: `24,143` rows across `5` benchmark/index symbols

### Completed full-provider checkpoint

Completed on-disk checkpoint after the full-provider rerun on `2026-05-07`:

- `tickers_INDX_UK_EU.parquet`: `218` filtered benchmark/index rows
- `prices_daily.parquet`: `902,515` rows across `218 / 218` benchmark/index pairs
- observed price date range: `2005-01-01` -> `2026-05-07`
- `prices_fetch_state.csv`: `218` rows
  - `218` = `up_to_date`

Interpretation:

- the filtered UK/EU direct-reference index sleeve is now fully materialized locally,
- the smoke benchmark subset remains useful for tiny validation reruns,
- and the current rerun behavior is fully incremental, with no full-history re-download required when the local sidecar already covers the requested upper bound.

### Current sidecar semantics

- `prices_fetch_state.csv`: `up_to_date` means the local history already covered the requested `--to` bound and no HTTP request was needed on that rerun.

### QC remediation note (2026-05-07)

After the initial QC report surfaced structural price anomalies, the UK/EU subset below was re-fetched with targeted `--tickers ... --full-refresh` repairs:

- `SCXP.INDX`
- `SX3R.INDX`
- `SX6R.INDX`
- `SXER.INDX`
- `SXIR.INDX`
- `SXKR.INDX`

Observed result after the targeted repairs and a second QC pass:

- the same structural anomalies remained present,
- and the UK/EU filtered reference sleeve now treats the following subset as known persistent provider-side price exceptions.

Current known persistent UK/EU index-reference price exceptions:

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
- continue normal incremental reruns for the broader filtered UK/EU reference sleeve,
- and only revisit the exception list after provider-side changes are observed or after adding explicit sanitation / exclusion handling downstream.

