---
id: INV-002
title: Schedulable command capability inventory
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.1
sources:
  - KB-001
  - KB-002
depends_on: [KB-001, KB-002, GLOSSARY]
referenced_by: [INV-001, INV-003, DD-001, ADR-003, OQ-003]
---

# Schedulable command capability inventory

This inventory is the exhaustive policy boundary for commands known at
substrate v1.0. A command absent from this table is unsupported. `CORE`,
`OPTIONAL`, `DEFER` and `FORBID` are the accepted OQ-003 classifications.

## Capability matrix

| Canonical command | Effect | Credentials/network | Resource access | Proposed class | Preconditions |
|---|---|---|---|---|---|
| `eodhd refresh ... --run` | incrementally mutates EODHD data | EODHD key; network; paid quota | exclusive `eodhd-data-root` | CORE | parser validation; key; writable root; non-interactive mode |
| `eodhd reindex` | rebuilds local query catalogue | none | exclusive `eodhd-data-root` | CORE | data root exists; DuckDB available |
| `eodhd status ...` | reads coverage/status; optional status-file write depends on flags | none | shared read, or exclusive if a write flag is admitted | OPTIONAL | reject mutating flags until explicitly classified |
| `eodhd qc ...` | reads and reports quality | none | shared `eodhd-data-root` | OPTIONAL | output must be log-safe |
| `macro fetch ... --run` | incrementally mutates macro parquet; not included by current EODHD-root sync | FRED/EODHD key; network | exclusive `macro-data-root` | OPTIONAL | selected provider credentials; writable root; no implied backup |
| `macro status` | reads macro coverage | none | shared `macro-data-root` | OPTIONAL | macro root resolves |
| `sync push ... --run` | pushes through configured sync backend | Drive token/network or local destination | shared `eodhd-data-root`; exclusive sync manifest/backend target; exclusive credential cache when refresh can write | CORE | non-interactive auth; backend configuration; source root exists |
| `sync status ...` | local scan vs local manifest | none | shared `eodhd-data-root` | OPTIONAL | source root and backend config resolve |
| `score plan ...` | reads scoring state and estimates work | model metadata; normally local | shared `eodhd-data-root` | DEFER | scoring configuration |
| `score run ... --run` | writes scores/embeddings; may spend budget | model service and possible paid budget | exclusive score outputs; shared corpus | DEFER | explicit budget policy and model health checks |
| `score status` | reads scoring state | none | shared `eodhd-data-root` | DEFER | scoring dependencies installed |
| `eodhd probe ...` | paid ad-hoc probe and cache writes | EODHD key; network; paid quota | probe cache write | DEFER | not a routine job; needs a separate quota policy |
| `sync login` | interactive browser OAuth | Google; browser | token write | FORBID | must be performed manually before scheduling |
| `eodhd config ...` | changes machine/repository configuration | varies | config write | FORBID | configuration is not scheduled work |
| exploratory commands (`describe`, `find`, `rows`, `coverage`, `sql`) | interactive/user-directed reads | none | shared read | FORBID | schedule reports can be designed separately if needed |
| lab/agent/investigation commands | interactive and model-directed | model-dependent | variable | FORBID | no deterministic headless contract |
| arbitrary executable, shell, Python or PowerShell | unconstrained | unconstrained | unconstrained | FORBID | outside the datacli command registry |

## Canonical namespace

Scheduled commands use an explicit family even when the interactive shell has a
shortcut:

- `eodhd refresh`, not ambiguous bare `refresh`;
- `eodhd reindex`;
- `macro fetch`;
- `sync push`;
- `score run` only if later admitted.

The family and verb are typed fields in DD-001. Remaining arguments are an argv
array and are parsed by the owning command adapter.

## Validation rules

1. Reject a family/verb not admitted by this inventory.
2. Reject shell metacharacter semantics; arguments remain literal argv values.
3. Require the command's own execution opt-in (`--run`) for a scheduled
   mutating operation. A plan-only schedule needs an explicit future policy;
   accidental recurring dry-runs are rejected.
4. Reject known interactive commands and flags.
5. Run command-specific static validation at schedule creation and again at
   execution, because code and configuration may change between them.
6. Run environment preflight before the first mutating step.
7. Redact secret-shaped values from definitions and logs; the preferred model
   contains no secrets to redact because they remain in existing stores.
8. Derive resource locks from the resolved command, not user-supplied strings.
9. Resolve and compare non-secret runtime bindings (data root, macro root,
   backend/target and configuration identity) against the validated definition.
10. Return a typed `CommandResult`; do not infer success, no-op or partial work
    by parsing human-oriented stdout.
11. A mutating command is not fully admitted until its direct interactive and
    headless consumers acquire the same canonical resource locks as the runner.
12. Resource locks use a same-user machine-wide namespace across profiles and
    checkouts. Credential-cache identity is a safe path/hash, never secret data.
13. Keep static syntax/schema validation, non-interactive readiness checks and
    runtime/live preflight distinct. Creation/enable performs no paid work and
    need not prove transient network reachability.
14. A failed/cancelled command reports whether mutation was none, partial,
    complete or unknown and supplies evidence-based retry guidance. V1 never
    turns that guidance into an automatic retry.
15. Preflight is domain-non-mutating. Runtime preflight may observe the network
    but cannot refresh/write a token; credential refresh belongs to execution
    under the credential-cache resource lock.
16. Freeze validated non-secret bindings into the execution context. Legacy
    subprocess adapters must receive explicit environment/arguments and cannot
    independently re-resolve a changed config target between workflow steps.

## Workflow rules

- A single scheduled command is represented as a one-step workflow.
- Steps run in listed order within one runner process.
- Default failure policy is stop-on-first-failure.
- A later step never starts after a failed or cancelled earlier step.
- Resource locks cover the workflow's combined declared access.
- Resource identities canonicalise case/path aliases and reject unsafe
  source/destination containment where applicable.
- Repeated commands remain explicit steps; there is no hidden lifecycle magic.
- `refresh -> reindex -> sync push` is a recommended composition, not a special
scheduler primitive.
- A run performs no implicit retry. Safe manual/recovery repetition depends on
  each mutating command's documented incremental/idempotent boundary.

## Implemented capability notes

- `sync push` has an explicit non-interactive authentication mode.
- Direct admitted mutating command paths and config writes use the shared lock
  manager.
- EODHD refresh and sync push fail-fast summaries distinguish unattempted work.
- Registry adapters return typed results; legacy integer exits remain captured
  evidence and human prose is not parsed.
- Macro data needs its own admitted backup command/target before any workflow
  claims that `sync push` backs it up.

## Inventory oracle

The eventual `schedule commands` output and command registry must match this
inventory mechanically. A parity test must fail when registry commands are
missing here, when `CORE` commands have no adapter, or when a `FORBID` command
can be installed.
