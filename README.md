# datacli

An interactive data-operations shell and data-acquisition toolkit, extracted from
[`btest`](../btest). It manages downloading, refreshing, and quality-checking raw
market data from external providers.

## Layout

- `datacli.py` — the interactive shell (a cmd2 + Rich REPL).
- `eodhd/` — the EODHD toolkit: registry-driven fetchers, unified CLI, status
  reporter, QC, and the bulk fast-refresh path. See `eodhd/README.md`.
- `tests/` — unit tests.

## Setup

```powershell
uv sync --extra dev
```

## The shell

```powershell
uv run python datacli.py
```
```
data>  /sources                 # list data sources
data>  /source eodhd            # enter a source context -> eodhd>
eodhd> /status                  # what data we have, as of when
eodhd> /fetch --fast --run      # bulk refresh (minutes, not hours)
eodhd> /qc                      # raw-data quality report
```
The leading `/` is optional. `help` lists commands; `quit` exits.

## EODHD CLI directly

```powershell
uv run python eodhd/cli.py status
uv run python eodhd/cli.py refresh --fast --run
```
See `eodhd/README.md` for the full runbook.

## Where the data lives

The raw EODHD snapshots (~GBs) stay in the `btest` sibling repo for now. By
default the fetchers read/write `../btest/data/raw/eodhd`. Override with:

```powershell
$env:EODHD_DATA_ROOT = "D:\somewhere\data\raw\eodhd"
```

## Sources

- **eodhd** — full operational tooling (status / fetch / qc / lanes / probe / config).
- **fred / yahoo / csv / parquet** — load adapters exist in `btest`; standalone
  operational plugins here are a planned follow-up.

## Provenance

Extracted from `btest` (`scripts/eodhd/` + the datacli shell). `btest` keeps the
backtesting framework and its runtime data adapters; this repo owns data
acquisition.
