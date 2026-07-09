# EODHD Fundamentals Refresh — Design

**Status:** DESIGN / IN IMPLEMENTATION
**Created:** 2026-07-07
**Scope:** `fetch_eodhd_us_fundamentals.py`, `fetch_eodhd_eu_fundamentals.py`, shared
`fundamentals_refresh_common.py`, and the `eodhd` CLI / status wiring.

## 1. Problem

The fundamentals fetchers are **append-only**. On each run they load the firms
already present in `fundamentals_quarterly.parquet` and *skip* them entirely
(`fetch_eodhd_us_fundamentals.py:384`, `fetch_eodhd_eu_fundamentals.py:777`):

```python
needs_fundamentals = (ticker, exchange) not in cached_tickers
...
if not needs_fundamentals and not needs_metadata and not missing_section_outputs:
    total_cached += 1
    continue
```

Consequences:

- Existing firms are **never updated** — new quarterly filings and restatements
  are never pulled.
- `--refresh-raw` does **not** help: it only bypasses the private JSON payload
  cache *for firms that are fetched*, and skipped firms never reach that code.
- Fundamentals is the **only lane with no `*_fetch_state.csv` sidecar**, so there
  is no per-firm freshness signal; `status_eodhd.py` falls back to the parquet's
  `filing_date` / file mtime.
- The exchange ticker lists are read from cache, so newly listed firms are not
  discovered on a plain re-run.

## 2. What already works (verified)

- **Keyed upsert exists.** `_merge_output_frame(existing, new, key_columns=[...])`
  merges new rows over old, de-duplicating on keys and keeping the latest. The
  main panel merges on `["ticker", "exchange", "statement", "date"]`. So
  **re-fetching a firm and running it through the existing merge already produces
  a correct upsert** — new quarters are added, restated quarters are overwritten.
  The *only* blocker is the skip.
- **`/calendar/earnings` is available on our key.** A `from`/`to` query returns
  `{"earnings": [{"code": "AAPL.US", "report_date": "...", "date": "<fiscal>"},
  ...]}`. A ~2-week window returned ~1,164 rows globally (~415 `.US`). This lets us
  target only firms that actually reported since the last pull.

## 3. The fix — three layers

### A. Update/upsert mode (correctness)
Add a refresh mode that, for *targeted* firms, does not skip: it re-fetches the
payload (bypassing the JSON cache), re-materializes, and lets the existing merge
upsert it. This alone makes refresh correct.

### B. Targeting (cost control)
EODHD bills fundamentals at ~10 API units/call, so re-fetching all ~6k US + ~6k
UK/EU firms (~120k units) every time is wasteful. Target instead:

- **Calendar-driven (default for `--update`)**: query `/calendar/earnings` for
  `[last_pull, today]`, take the reported `(ticker, exchange)` set, and refresh
  only firms that reported (plus any firm not yet present).
- **Stale-days fallback (`--stale-days N`)**: if the calendar is unavailable or
  disabled, refresh firms whose sidecar `fetched_at` is older than `N` days.
- **Full (`--full-refresh`)**: refresh everyone — for restatement sweeps / a
  periodic hard rebuild.

### C. State sidecar (freshness + incremental)
Write `fundamentals_fetch_state.csv` next to the parquet, one row per firm:

| column | meaning |
|---|---|
| `ticker`, `exchange` | firm identity |
| `status` | `ok` / `empty` / `error` |
| `fetched_at` | UTC timestamp of the last successful fetch |
| `latest_filing_date` | max `filing_date` for the firm |
| `latest_statement_date` | max statement `date` for the firm |
| `n_quarters` | quarterly statement rows for the firm |
| `detail` | optional note |

This gives `status_eodhd.py` a real freshness anchor (mirrors prices/events) and
drives the `--stale-days` gate. The registry's `fundamentals_spec` already points
`freshness_col` at `filing_date`; it will be repointed at the sidecar.

## 4. Targeting logic (pure, testable)

`select_targets` in `fundamentals_refresh_common.py`:

```
full      -> all candidates
backfill  -> candidates not already present            (legacy default)
update    -> (candidates not present)
             ∪ (present ∧ reported)      if calendar available
             ∪ (present ∧ stale(N days)) otherwise
```

`stale(firm)` is true when the firm has no sidecar row or its `fetched_at` is
older than `as_of - stale_days`. Kept side-effect free so it unit-tests against
fixtures with no live calls.

## 5. Modes & flags (both fetchers)

| flag | effect |
|---|---|
| *(none)* | `backfill` — legacy behavior: only firms not yet present |
| `--update` | calendar-targeted refresh of reported + new firms (stale-days fallback) |
| `--stale-days N` | with `--update`, force the time-based gate (default when no calendar) |
| `--full-refresh` | refresh every firm |
| `--refresh-raw` | (existing) bypass the JSON payload cache; implied for targeted firms |
| `--no-calendar` | disable calendar targeting even in `--update` |

## 6. Integration

- **Registry** (`eodhd_datasets.py`): `fundamentals_spec` gains
  `state="fundamentals_fetch_state.csv"`, `freshness_col` from the sidecar.
- **Status** (`status_eodhd.py`): fundamentals now reads the sidecar like the
  other lanes (as-of, coverage-less, fetched, status counts) instead of mtime.
- **CLI** (`cli.py`): `eodhd refresh --datasets fundamentals` runs the fetchers
  with `--update`; `--full-refresh` forwards through (fundamentals is exempt from
  the incremental-only passthrough guard for this one flag).

## 7. Cost note

At ~10 units/call: a calendar-targeted weekly `--update` touches only the handful
of firms that reported that week (near-zero most weeks; a few hundred in peak
earnings season) versus ~120k units for a blind full sweep. `--full-refresh` is
reserved for deliberate restatement audits.

## 8. Rollout / testing

- Unit tests (fixtures, **no live API**): `select_targets` across modes, sidecar
  load/write round-trip, per-firm summary extraction.
- Live smoke: `--update --tickers AAPL.US MSFT.US` confirms fetch → upsert →
  sidecar end-to-end without a full sweep.
- The existing append-only default is unchanged, so initial bulk loads and
  interrupted-run resumes behave exactly as before.
