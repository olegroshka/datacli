---
id: KB-002
title: What a datacli node is today, and what in it is inherently single-node
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.2
sources:
  - Source inspection of eodhd/, storage/, scheduler/ on 2026-09-12
  - docs/scheduler-initiative/KB-002 and DD-001
  - docs/SCHEDULER_DAILY_RUN_RECOVERY.md
  - SESSION-001-PREP section 2 (code findings F1 to F22, 2026-09-12) and 10.1 (devices, owner 2026-09-13)
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
| Scoring | `score run` writes day partitions `news/scores/<schema>@<v>/<backend-id>/YYYY-MM-DD.parquet` and `news/embeddings/<model-id>/YYYY-MM-DD.parquet`, each with provenance columns (schema, version, backend, model, prompt hash), upserted atomically on `(article_id, symbol)`; one shared `state.csv` per sidecar directory, rewritten after every chunk. Selects by day (`--days/--since/--until`), resumes per chunk, refuses paid models at `budget_usd = 0`; models via LiteLLM to Ollama (single-stream). Deferred from scheduling by INV-002. This is the GPU-shaped work. |
| Storage | `storage/engine.py` plans a push from a local scan against one manifest per (data root, backend) at `<data_root>/.sync/<backend>.json` (entries `size, mtime_ns, md5, remote_id, uploaded_at`); backends implement `describe, ensure_auth, upload`, optional `list_remote`/`trash`; no download path exists. Drive backend uses `drive.file` scope and a per-device OAuth token; folder creation is list-then-create (not atomic). Push is one-way (local is truth) with include globs but no write scope; `reconcile` rebuilds a manifest from the remote listing by md5. |
| Scheduler | Registry of allowlisted commands with derived resource claims; runner with per-run journal, awake-clock timeout, keep-awake, notifications; same-user machine-wide file locks; Windows Task Scheduler adapter (S4U or interactive principal); three state planes (desired, backend, execution). |
| Agents | `lab`/`agent`/investigation commands exist for interactive use and are FORBID for scheduling (no deterministic headless contract). The MCP server exposes three read-only tools (`sql`, `describe_schema`, `list_lanes`) over stdio. Model tiers (`local, cheap, mid, strong, embed-local, embed-cheap`) live in `llm/tiers.py`; "local" is currently the `ollama/` prefix. |
| Config and secrets | `datacli.toml` (data root, sync backend, notify command); EODHD key in user env; Drive token file under the user profile. Config content is fingerprinted into job bindings. Node identity today is the scheduler `profile_id`, a UUID bound to (repo root, config path); no machine id exists in any record. Contract versions exist (`scheduler-commands-v1`, job schema 1, manifest 1, scoring `schema@v`). Every `refresh --run` ends with `status --write`, producing `STATUS.json` per node. |

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
| Scores and embeddings are day partitions already; only their per-directory `state.csv` is shared across days. | The GPU unit `(schema@v, model, day)` and its one-writer file exist; `state.csv` must become node-local and never pushed. (OQ-002, OQ-004) |
| The news daily tables (`news_symbol_daily`, `news_issuer_daily`, `issuer_map`) are single-file snapshots rebuilt locally from the article partitions. | Local builds are not fleet units; every consuming node rebuilds its own. (OQ-002) |
| Locks are OS-held byte-range locks with no lease or TTL; crash release is automatic. | The fleet lease is the first time-based ownership concept in the codebase; it lives in the consensus layer, never in the lock manager. (OQ-002) |
| `Capability` has `mutation, network, requires_run` but no `paid` or `model` flag; INV-002 classes live only in the doc. | Fleet units need `paid` and `model` on the capability; `score run` and `score bench` must be admitted. (OQ-002) |

## 3. What already helps

- Partition-by-day news articles: a natural slice and a natural one-writer
  file.
- Idempotent, md5-aware push and the reconcile path: a base for
  "one current copy".
- Registry allowlist and journal: the execution half of G5 is done.
- Contract versions and definition digests: a base for detecting version skew
  (RN3).
- Fakes culture: the local backend and in-process runner tests can grow into a
  multi-node simulation (RN4). Fakes exist for every port except the ledger:
  `FakeBackend`, `LocalBackend`, fake awake clock and power, scripted registry
  results.
- The notification path (`LAST_RUN.txt`, `LAST_FAILURE.txt`,
  `[scheduler] notify_command`) can carry fleet failures unchanged (RN5).
- Git 2.52 is installed; no git library is a dependency, so a git ledger can
  be a subprocess adapter and a local bare repository gives real
  compare-and-set semantics in tests.

## 4. Devices available to the fleet (owner, 2026-09-13)

| Device | Hardware | Holds | Availability | Standing role (D1) |
|---|---|---|---|---|
| Windows box (this node) | small GPU (12 GB class per README) | EODHD key, Drive token, Claude and OpenAI subscriptions; daily job at 05:00 under S4U | at home, sleeps | pinned fetcher for every lane; owner's console and approval point; subscription harness host |
| Linux box | RTX 5090, 32 GB VRAM | (to be provisioned: deploy key, Drive token if it pushes) | at home, not always on | primary scorer; full mirror of the data root; batched local model endpoint |
| Legion laptop | RTX 5090, 24 GB VRAM | (to be provisioned) | travels, sleeps | opportunistic scorer; claims only when docked and on AC |

Secrets never cross devices (G9): each device that pushes gets its own Drive
token; each device gets its own ledger deploy key; only the Windows box holds
the EODHD key in v1.
