# EODHD US ETF Data Manifest

**Status:** DRAFT / FACTUAL-INVENTORY / FULL-PROVIDER-COMPLETE  
**Created:** 2026-05-06  
**Purpose:** btest-owned operational inventory for the US ETF EODHD workflow  
**Source provenance:** follows the completed US common-stock workflow in `eodhd/`, but as a separate sleeve

## 1. Scope

This file is the `btest`-local execution note for the US ETF lane under EODHD.

The ETF lane now defaults to the full provider ETF universe from EODHD's US exchange-symbol list, while preserving the original curated starter basket as an explicit small-sleeve mode for faster smoke runs and debugging.

Starter basket:

- `SPY.US`
- `QQQ.US`
- `IWM.US`
- `DIA.US`
- `TLT.US`
- `IEF.US`
- `LQD.US`
- `HYG.US`
- `GLD.US`
- `XLK.US`
- `XLF.US`
- `XLE.US`
- `XLV.US`

## 2. Planned local paths in `btest`

Primary landing zone:

- `data/raw/eodhd/us_etf/tickers_US_ETF.parquet`
- `data/raw/eodhd/us_etf/starter_universe.csv`
- `data/raw/eodhd/us_etf/prices_daily.parquet`
- `data/raw/eodhd/us_etf/prices_fetch_state.csv`
- `data/raw/eodhd/us_etf/dividends_history.parquet`
- `data/raw/eodhd/us_etf/dividends_fetch_audit.csv`
- `data/raw/eodhd/us_etf/dividends_fetch_state.csv`
- `data/raw/eodhd/us_etf/splits_history.parquet`
- `data/raw/eodhd/us_etf/splits_fetch_audit.csv`
- `data/raw/eodhd/us_etf/splits_fetch_state.csv`

## 3. Current observed scope and completion state

### Current observed live scope (2026-05-07)

Observed live counts from the EODHD `exchange-symbol-list/US` endpoint for `Type` containing `ETF`:

- provider ETF list rows saved to `tickers_US_ETF.parquet`: `5,543`
- curated starter basket rows saved to `starter_universe.csv`: `13`

Current targeting rule in code:

- default ETF target universe for `fetch_eodhd_us_etf_prices.py`, `fetch_eodhd_us_etf_dividends.py`, and `fetch_eodhd_us_etf_splits.py`: full provider ETF universe (`5,543` rows as observed on 2026-05-06)
- optional narrow mode: `--universe starter` to restrict the run back to the curated `13`-ETF sleeve
- explicit `--tickers ...` continues to override both universe modes

Starter basket members currently materialized locally:

- `DIA.US`
- `GLD.US`
- `HYG.US`
- `IEF.US`
- `IWM.US`
- `LQD.US`
- `QQQ.US`
- `SPY.US`
- `TLT.US`
- `XLE.US`
- `XLF.US`
- `XLK.US`
- `XLV.US`

### Completed starter-sleeve checkpoint

Completed starter-sleeve checkpoint on `2026-05-06`:

- live ETF universe refresh completed successfully
- live ETF price/dividend/split workflows completed across the full curated `13`-ETF starter sleeve
- final audits confirmed full starter-sleeve coverage in prices plus all dividend/split audit/state sidecars
- after that checkpoint, the ETF fetch scripts were widened so the default production target is now the full provider ETF universe rather than only the starter sleeve

Observed on-disk completed starter-sleeve results:

- `prices_daily.parquet`: `69,214` rows across `13` ETFs
- observed ETF price date range: `2005-01-03` -> `2026-05-05`
- `prices_fetch_state.csv`: `5 ok` (`full` fetch on the new names), `8 up_to_date` (`skip` on the already-bootstrapped names)
- `dividends_history.parquet`: `2,157` rows across `12` ETFs
- observed ETF dividend date range: `1993-03-19` -> `2026-05-01`
- `dividends_fetch_audit.csv`: `12 ok`, `1 empty` (`GLD.US`)
- `dividends_fetch_state.csv`: `13` starter ETFs covered with explicit per-ticker status rows
- `splits_history.parquet`: `5` rows across `5` ETFs
- observed ETF split date range: `2000-03-20` -> `2025-12-05`
- `splits_fetch_audit.csv`: `5 ok`, `8 empty`
- `splits_fetch_state.csv`: `13` starter ETFs covered with explicit per-ticker status rows

Interpretation:

- the ETF starter sleeve is now fully materialized locally,
- the curated basket resolves cleanly against the provider ETF universe and reaches `13/13` coverage for prices and all event sidecars,
- dividend history is present for `12/13` names, with `GLD.US` currently returning an empty dividend history,
- split history remains sparse, but the sidecars preserve explicit `empty` states for starter ETFs with no returned split records,
- and that starter checkpoint became the basis for the later resumable expansion over the full provider ETF list.

### Completed full-provider checkpoint

Completed on-disk full-provider checkpoint after the recovered reruns on `2026-05-07`:

- `prices_daily.parquet`: `8,362,770` rows across `5,543 / 5,543` ETF pairs
- observed ETF price date range: `2005-01-03` -> `2026-05-05`
- `prices_fetch_state.csv`: `5,543` rows
  - `5,530` = `ok`
  - `13` = `up_to_date`
- `dividends_history.parquet`: `152,000` rows across `4,415` ETFs with non-empty dividend history
- observed ETF dividend ex-date range: `1986-10-08` -> `2026-05-06`
- `dividends_fetch_audit.csv`: `5,543` audited ETF pairs
  - `4,415` = `ok`
  - `1,128` = `empty`
- `dividends_fetch_state.csv`: `5,543` rows with full pair coverage
  - `4,415` = `ok`
  - `1,128` = `empty`
- `splits_history.parquet`: `1,136` rows across `678` ETFs with non-empty split history
- observed ETF split ex-date range: `1981-06-17` -> `2026-05-05`
- `splits_fetch_audit.csv`: `5,543` audited ETF pairs
  - `678` = `ok`
  - `4,865` = `empty`
- `splits_fetch_state.csv`: `5,543` rows with full pair coverage
  - `678` = `ok`
  - `4,865` = `empty`

Interpretation:

- the full provider US ETF universe is now materialized locally for prices, dividends, and splits,
- the starter sleeve remains available as an explicit smoke/debug mode via `--universe starter`,
- prices currently cover every target pair,
- dividend and split outputs remain sparse by nature, but their sidecars now explicitly preserve both `ok` and `empty` outcomes for all `5,543` target pairs,
- and normal reruns should continue incrementally from the persisted state sidecars without requiring manual ticker bookkeeping.

### Current sidecar semantics

- `prices_fetch_state.csv`: `ok` means the script fetched and merged history on that run; `up_to_date` means the pair was already covered through the requested `--to` bound and no HTTP call was needed.
- `dividends_fetch_state.csv` / `splits_fetch_state.csv`: `ok` means provider event history was returned; `empty` means the provider returned no event history for that pair.
- `dividends_fetch_audit.csv` / `splits_fetch_audit.csv` mirror the event outcome inventory and are useful for quick operator audits.

### QC remediation note (2026-05-07)

After the initial QC report surfaced structural price anomalies, the following symbols were re-fetched with targeted `--tickers ... --full-refresh` repairs:

- `PSQA.US`
- `SSSEF.US`
- `VYLD.US`

Observed result after the targeted repairs and a second QC pass:

- the same symbols remained flagged for `non_positive_prices`,
- which indicates that these rows currently behave like persistent provider-returned oddities rather than local resume / merge corruption.

Current known persistent US ETF price exceptions:

- `PSQA.US` -> `non_positive_prices`
- `SSSEF.US` -> `non_positive_prices`
- `VYLD.US` -> `non_positive_prices`

Operator rule:

- do **not** keep re-running routine targeted full-refresh repairs for these exact symbols,
- continue normal incremental lane reruns for the rest of the universe,
- and only revisit these names after either provider-side changes are observed or code-level sanitation rules are introduced for explicitly handling them downstream.

## 4. Maintenance rule

Update this manifest when the US ETF workflow changes materially, especially when any of the following move:

- ETF universe definition,
- output paths,
- row counts,
- sidecar semantics,
- starter basket membership,
- or call-budget/runtime expectations.

