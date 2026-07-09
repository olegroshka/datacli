# EODHD UK/EU ETF Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / FULL-RUN-COMPLETE  
**Created:** 2026-05-06  
**Purpose:** btest-owned operational inventory for the UK/EU ETF EODHD workflow  
**Source provenance:** separate ETF sleeve alongside the completed UK/EU common-stock lane in `eodhd/`

## 1. Scope

This file is the `btest`-local execution note for the UK/EU ETF lane under EODHD.

Current target universe:

- the existing UK/EU exchange set from `fetch_eodhd_eu_fundamentals.py`,
- filtered to provider rows whose `Type` contains `ETF`,
- kept separate from the UK/EU common-stock issuer universe.

## 2. Planned local paths in `btest`

Primary landing zone:

- `data/raw/eodhd/uk_eu_etf/tickers_UK_EU_ETF.parquet`
- `data/raw/eodhd/uk_eu_etf/prices_daily.parquet`
- `data/raw/eodhd/uk_eu_etf/prices_fetch_state.csv`
- `data/raw/eodhd/uk_eu_etf/dividends_history.parquet`
- `data/raw/eodhd/uk_eu_etf/dividends_fetch_audit.csv`
- `data/raw/eodhd/uk_eu_etf/dividends_fetch_state.csv`
- `data/raw/eodhd/uk_eu_etf/splits_history.parquet`
- `data/raw/eodhd/uk_eu_etf/splits_fetch_audit.csv`
- `data/raw/eodhd/uk_eu_etf/splits_fetch_state.csv`

## 3. Current observed scope and completion state

1. refresh the provider UK/EU ETF universe across the existing target exchanges
2. run a small explicit ETF smoke pull
3. run the full-provider UK/EU ETF prices / dividends / splits jobs
4. audit final row counts, pair coverage, and date ranges

### Current observed live scope (2026-05-07)

Observed provider UK/EU ETF universe after the first live run:

- provider UK/EU ETF rows saved to `tickers_UK_EU_ETF.parquet`: `8,676`
- observed exchange counts:
  - `XETRA`: `3,276`
  - `LSE`: `3,177`
  - `SW`: `1,254`
  - `PA`: `558`
  - `AS`: `370`
  - `ST`: `27`
  - `MC`: `5`
  - `VI`: `5`
  - `CO`: `2`
  - `HE`: `1`
  - `OL`: `1`

Completed live smoke checkpoint on `2026-05-06`:

- `fetch_eodhd_uk_eu_etf_prices.py --limit 5 --full-refresh`
- `fetch_eodhd_uk_eu_etf_dividends.py --limit 5 --full-refresh`
- `fetch_eodhd_uk_eu_etf_splits.py --limit 5 --full-refresh`

Observed smoke results:

- `prices_daily.parquet`: `3,186` rows across `5` ETFs
- `dividends_fetch_audit.csv`: `5 empty`
- `splits_fetch_audit.csv`: `5 empty`

### Completed full-provider checkpoint

Completed on-disk checkpoint after the recovered reruns on `2026-05-07`:

- `prices_daily.parquet`: `15,599,415` rows across `8,676 / 8,676` ETF pairs
- observed ETF price date range: `2005-01-03` -> `2026-05-07`
- `prices_fetch_state.csv`: `8,676` rows
  - `1,277` = `ok`
  - `7,399` = `up_to_date`
- `dividends_history.parquet`: `64,603` rows across `2,747` ETFs with non-empty dividend history
- observed ETF dividend ex-date range: `2000-08-29` -> `2026-05-07`
- `dividends_fetch_audit.csv`: `8,676` audited ETF pairs
  - `2,747` = `ok`
  - `5,929` = `empty`
- `dividends_fetch_state.csv`: `8,676` rows with full pair coverage
  - `2,747` = `ok`
  - `5,929` = `empty`
- `splits_history.parquet`: `213` rows across `187` ETFs with non-empty split history
- observed ETF split ex-date range: `2005-11-23` -> `2026-04-20`
- `splits_fetch_audit.csv`: `8,676` audited ETF pairs
  - `187` = `ok`
  - `8,489` = `empty`
- `splits_fetch_state.csv`: `8,676` rows with full pair coverage
  - `187` = `ok`
  - `8,489` = `empty`

Recovery note from the interrupted session:

- the only remaining uncovered provider pair after the resumed bulk runs was `MAJMEL.CO`,
- a direct targeted retry on `2026-05-07` completed the price lane for that symbol,
- and the same targeted retry recorded `empty` outcomes for its dividend and split lanes.

Interpretation:

- the UK/EU ETF provider universe is now fully materialized locally for prices and fully audited for dividends and splits,
- prices cover every target pair,
- dividend and split histories are sparse by nature, but their sidecars now preserve explicit `ok` and `empty` outcomes for every target pair,
- and normal reruns should continue incrementally from the existing sidecars rather than rebuilding the lane.

### Current sidecar semantics

- `prices_fetch_state.csv`: `ok` means the run fetched and merged history for that pair; `up_to_date` means the pair was already covered through the requested `--to` bound and no HTTP request was needed on the rerun.
- `dividends_fetch_state.csv` / `splits_fetch_state.csv`: `ok` means provider event history was returned; `empty` means the provider returned no event history for that pair.
- `dividends_fetch_audit.csv` / `splits_fetch_audit.csv` mirror the same per-pair outcome inventory for operator audits.

### QC remediation note (2026-05-07)

After the initial QC report surfaced structural price anomalies, the following symbols were re-fetched with targeted `--tickers ... --full-refresh` repairs:

- `PHPT.AS`
- `EX14.VI`

Observed result after the targeted repairs and a second QC pass:

- `PHPT.AS` remained flagged for `invalid_ohlc_relationship` and `non_positive_prices`,
- `EX14.VI` remained flagged for `non_positive_prices`,
- which indicates that these rows currently behave like persistent provider-returned oddities rather than local resume / merge corruption.

Current known persistent UK/EU ETF price exceptions:

- `PHPT.AS` -> `invalid_ohlc_relationship`, `non_positive_prices`
- `EX14.VI` -> `non_positive_prices`
- `EXHG.XETRA` -> `invalid_ohlc_relationship` (2,327 historical bars; survives `--full-refresh` as of 2026-07-09)

Operator rule:

- do **not** keep re-running routine targeted full-refresh repairs for these exact symbols,
- continue normal incremental lane reruns for the rest of the ETF universe,
- and only revisit these names after either provider-side changes are observed or explicit sanitation / exclusion handling is added downstream.

