---
id: KB-002
title: What a datacli node is today, and what in it is inherently single-node
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - Source inspection of eodhd/, storage/, scheduler/ on 2026-09-12
  - docs/scheduler-initiative/KB-002 and DD-001
  - docs/SCHEDULER_DAILY_RUN_RECOVERY.md
depends_on: [GLOSSARY]
referenced_by: [BOOTSTRAP-FLEET, INV-001, OQ-001, OQ-002, OQ-004]
---

# KB-002 - The node as it exists

Facts, grounded in code as of 2026-09-12. The last section lists what each fact
implies for a fleet; those implications feed the open questions.

## 1. Components of one node

| Component | Fact |
|---|---|
| Data estate | Lanes (us_common, uk_eu, us_etf, index_ref, uk_eu_etf, uk_eu_index_ref, news) under one local data root; per lane: prices/dividends/splits/fundamentals parquet plus `*_fetch_state.csv` and `*_fetch_audit.csv` sidecars keyed by (ticker, exchange). News articles are partitioned by day (`news/articles/YYYY-MM-DD.parquet`); derived news tables and scores/embeddings are single snapshot files. |
| Fetching | Per-ticker incremental fetchers with a window derived from the sidecar; a bulk `--fast` path; a `refresh` orchestrator that runs lane steps in order and now ends with `status --write`. The pull universe is qualifying + previously tracked pairs (common lanes) or the provider universe file (ETF/index lanes). One EODHD API key, read from the user environment. |
| Local builds | `reindex` (catalogue), news daily/issuer tables: deterministic, no API. |
| Scoring | `score run` produces `news_scores` and `news_embeddings` snapshots; model-bound, budgeted; deferred from scheduling by INV-002. This is the GPU-shaped work. |
| Storage | `storage/engine.py` plans a push from a local scan against one manifest per (data root, backend); backends implement upload, optional `list_remote`/`trash`; Drive backend uses `drive.file` scope and per-device OAuth token. Push is one-way (local is truth); `reconcile` rebuilds a manifest from the remote listing by md5. |
| Scheduler | Registry of allowlisted commands with derived resource claims; runner with per-run journal, awake-clock timeout, keep-awake, notifications; same-user machine-wide file locks; Windows Task Scheduler adapter (S4U or interactive principal); three state planes (desired, backend, execution). |
| Agents | `lab`/`agent`/investigation commands exist for interactive use and are FORBID for scheduling (no deterministic headless contract). |
| Config and secrets | `datacli.toml` (data root, sync backend, notify command); EODHD key in user env; Drive token file under the user profile. Config content is fingerprinted into job bindings. |

## 2. What is inherently single-node today

| Fact | Implication for a fleet |
|---|---|
| Locks are files on one machine for one user. | Fleet-level mutual exclusion does not exist; two nodes can run the same fetch. (OQ-002) |
| One manifest per data root; a push assumes local is the truth for every file. | Two nodes pushing the same path would fight; "local is truth" must become "owner of the partition is truth". (OQ-004) |
| Fetch state sidecars are whole-lane CSVs rewritten by one fetcher. | A lane's state cannot be split across nodes without partitioning the sidecar or the lane. (OQ-002, OQ-004) |
| Sync is push-only. | A node cannot obtain inputs another node produced; scoring on node B needs news fetched by node A. (OQ-004) |
| Journal and run records live under one profile on one machine. | Fleet observability needs records to travel, or a fleet view that reads every node's store. (OQ-001) |
| The registry executes commands; nothing issues or receives work. | A claiming/leasing layer does not exist. (OQ-002) |
| Agents are interactive only. | A headless planner role with a bounded protocol is new. (OQ-003) |
| One API key, one quota, no budget ledger. | Global budgets need a shared counter or conservative static partitioning. (OQ-001, OQ-002) |
| Snapshot outputs (scores, embeddings, daily tables) are single files. | GPU work cannot be split unless these outputs become partitioned with a merge rule. (OQ-004) |

## 3. What already helps

- Partition-by-day news articles: a natural slice and a natural one-writer
  file.
- Idempotent, md5-aware push and the reconcile path: a base for
  "one current copy".
- Registry allowlist and journal: the execution half of G5 is done.
- Contract versions and definition digests: a base for detecting version skew
  (RN3).
- Fakes culture: the local backend and in-process runner tests can grow into a
  multi-node simulation (RN4).
