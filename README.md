# datacli

An interactive data-operations shell and data-acquisition toolkit, extracted from
[`btest`](../btest). It manages downloading, refreshing, and quality-checking raw
market data from external providers — and lets you explore it with fast ad-hoc
queries.

## Layout

- `datacli.py` — the interactive shell (a cmd2 + Rich REPL).
- `eodhd/` — the EODHD toolkit: registry-driven fetchers, unified CLI, status
  reporter, QC, DuckDB-backed explorer, and the bulk fast-refresh path. See
  `eodhd/README.md`.
- `tests/` — unit tests.

## Setup

```powershell
uv sync --extra dev
```

## Quickstart: point datacli at your own data

If you cloned this repo and already have EODHD raw snapshots on disk, three steps
get you from clone to answered question:

```powershell
uv sync --extra dev

# 1. tell datacli where your raw data lives (writes datacli.toml, git-ignored)
uv run python eodhd/cli.py config set data-root "D:\data\raw\eodhd"

# 2. build the query index (a small catalog parquet next to your data)
uv run python eodhd/cli.py reindex

# 3. ask a question
uv run python eodhd/cli.py describe VAR.OL
```

`config` with no argument shows the resolved data root and where it came from
(env var, config file, or the `../btest` default). Resolution precedence:

```
EODHD_DATA_ROOT env var  >  datacli.toml [eodhd].data_root  >  ../btest default
```

## The shell

```powershell
uv run python datacli.py
```
```
data>  /sources                 # list data sources
data>  /source eodhd            # enter a source context -> eodhd>
eodhd> /status                  # colour-coded as-of dashboard (all lanes)
eodhd> /status us_common        # ... scoped to one lane
eodhd> /fetch --fast --run      # bulk refresh (minutes, not hours)
eodhd> /qc                      # raw-data quality triage (all lanes)
eodhd> /qc us_common splits     # ... drill into one lane + dataset
```
The leading `/` is optional. `help` lists commands; `quit` exits. Explore verbs
(`describe`/`find`/`rows`/`coverage`/`sql`) run in-process against a warm DuckDB
connection, so repeated queries are snappy.

## Exploring the data

Fast ad-hoc queries over the raw parquet, no SQL required (each also works as a
direct `eodhd/cli.py <verb>`):

```
eodhd> describe VAR.OL              # which datasets cover this ticker, and how far
eodhd> find VAR                     # fuzzy-search tickers across all datasets
eodhd> rows VAR.OL dividends        # latest rows (narrow default columns)
eodhd> rows VAR.OL prices --cols *  # ... or all columns
eodhd> coverage VAR.OL              # per-dataset coverage windows
eodhd> sql "SELECT count(*) FROM dividends WHERE lane='us_common'"
```

`sql` runs read-only DuckDB against views named `prices`, `dividends`, `splits`,
`fundamentals` (and their `*_state` sidecars). Every view carries a `lane` column.

## Schema & versioning

The columns the tool relies on are declared in `eodhd/schema.py` with a
`SCHEMA_VERSION`. Queries stay stable as the data evolves via **projected views**:
each dataset view guarantees its canonical columns exist (aliasing a known rename
or NULL-filling a missing one) while passing every other column through. So data
written under an older or newer schema still queries cleanly.

```
eodhd> schema        # diff the declared schema against your on-disk columns
eodhd> reindex       # rebuild the query catalog after fetching new data
```

`schema` reports, per dataset, which canonical columns are present/missing and any
extra columns riding along — handy after a provider adds fields.

## EODHD CLI directly

```powershell
uv run python eodhd/cli.py status
uv run python eodhd/cli.py refresh --fast --run
```
See `eodhd/README.md` for the full runbook.

## Where the data lives

The raw EODHD snapshots (~GBs) stay in the `btest` sibling repo by default, so the
fetchers read/write `../btest/data/raw/eodhd`. Override per the Quickstart above
(`config set data-root ...`) or, for a one-off, with an env var:

```powershell
$env:EODHD_DATA_ROOT = "D:\somewhere\data\raw\eodhd"
```

## Sources

- **eodhd** — full operational tooling (status / fetch / qc / lanes / probe /
  config / describe / find / rows / coverage / sql / schema / reindex).
- **fred / yahoo / csv / parquet** — load adapters exist in `btest`; standalone
  operational plugins here are a planned follow-up.

## Provenance

Extracted from `btest` (`scripts/eodhd/` + the datacli shell). `btest` keeps the
backtesting framework and its runtime data adapters; this repo owns data
acquisition.
