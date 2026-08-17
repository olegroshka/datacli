---
id: DD-001
title: Scheduler domain records, ports and execution contracts
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - KB-001
  - KB-002
  - INV-002
depends_on: [KB-001, KB-002, INV-002, ADR-001, ADR-003, ADR-004, ADR-005, GLOSSARY]
referenced_by: [INV-001, INV-003, ADR-002, OQ-001, OQ-002, OQ-003]
---

# Scheduler domain records, ports and execution contracts

This dictionary is infrastructure-neutral. Windows-specific fields belong in
the Windows adapter or an explicitly namespaced extension, not in the common
records.

## Identity rules

- `profile_id`: generated UUID, stable and machine-local for one datacli
  checkout/config context, as resolved by OQ-001.
- `job_id`: lowercase user-visible ASCII slug unique within a profile;
  immutable after creation; excludes separators, dots, control characters and
  Windows-reserved names. Rename is create-new plus deprecate/delete-old.
- `run_id`: time-labelled collision-resistant identifier unique per invocation;
  wall-clock order is not authoritative if the system clock moves backwards.
- OS task identity is derived from `profile_id` and `job_id`; it is not a second
  user-facing identity. Adapter derivation is length-bounded and cannot be
  escaped by user input.

## Canonical records

### `JobDraft`

An editable composition record used by `schedule create` and multi-step edits.
It may have zero steps and may refer to a `base_generation`, but it is never
desired state, never installed and never executable. Finalise/apply validates
the whole draft and atomically commits one new `JobSpec` generation. Draft
discard and stale-base conflict are explicit.

### `JobSpec`

| Field | Type | Required | Invariant |
|---|---|---:|---|
| `schema_version` | integer | yes | recognised positive version |
| `profile_id` | string | yes | matches the active profile |
| `job_id` | slug string | yes | immutable and unique in profile |
| `generation` | positive integer | yes | monotonically increases on desired-state change |
| `display_name` | string | yes | non-empty; not identity-bearing |
| `enabled` | boolean | yes | desired state, reconciled with backend |
| `repo_root` | absolute path | yes | contains the expected datacli checkout |
| `interpreter` | absolute path | yes | executable Python runtime |
| `config_path` | absolute path or null | yes | explicit config identity; absence is explicit |
| `runtime_bindings` | non-empty list of `RuntimeBinding` | yes | non-secret resolved resources/targets at validation |
| `command_contract_version` | string/hash | yes | registry semantics used to validate steps |
| `trigger` | `TriggerSpec` | yes | supported by selected backend |
| `steps` | non-empty list of `CommandSpec` | yes | every step validates through INV-002 policy |
| `policy` | `ExecutionPolicy` | yes | internally consistent |
| `created_at` | UTC timestamp | yes | immutable |
| `updated_at` | UTC timestamp | yes | not earlier than `created_at` |

No secret, token, API key, Windows password or opaque shell command is a valid
field.

Each validated form is stored as an immutable content-addressed definition
snapshot. A separate atomic desired pointer selects the current
`generation`/digest or records a tombstone. A digest alone is not sufficient
history unless the referenced snapshot is retained.

### `TriggerSpec`

| Field | Type | Required | Invariant |
|---|---|---:|---|
| `kind` | `daily`, `weekly`, `manual` | yes | first-release enum |
| `local_time` | `HH:MM` or null | conditional | present for calendar triggers |
| `days_of_week` | ordered unique day enum list | weekly only | non-empty for weekly |
| `time_basis` | `system_local` | calendar only | first release follows the Windows local wall clock |
| `timezone_at_validation` | zone identifier | calendar only | diagnostic binding; change is reported as drift |
| `start_when_available` | boolean | yes | allows a delayed start; does not promise replay of every miss |
| `wake_to_run` | boolean | yes | never implied |
| `ac_only` | boolean | yes | never implied |

### `CommandSpec`

| Field | Type | Required | Invariant |
|---|---|---:|---|
| `family` | command-family enum | yes | registered and admitted by INV-002 |
| `verb` | string enum within family | yes | registered and admitted |
| `argv` | list of literal strings | yes | excludes family/verb; no shell interpretation |
| `contract_version` | string/hash | yes | exact adapter validation contract |
| `resources` | derived list of `ResourceClaim` | yes | registry-derived, not user-authored |
| `mutation` | boolean | yes | registry-derived |
| `network` | boolean | yes | registry-derived |

The persisted form may cache derived fields for observability, but execution
must re-resolve them and reject material drift.

### `RuntimeBinding`

| Field | Type | Required | Invariant |
|---|---|---:|---|
| `name` | enum/string | yes | e.g. `eodhd_data_root`, `macro_data_root`, `sync_target` |
| `resource_id` | stable string | yes | same canonical identity used for locks |
| `resolved_value` | path or non-secret target identity | yes | never a token, password or signed URL |
| `source` | enum/string | yes | config/env/default origin without secret value |
| `fingerprint` | hash or null | conditional | detects material config/target drift |

Runtime re-resolution compares these bindings before mutation. Environment
overrides do not silently redirect a scheduled job. After validation the runner
constructs a frozen non-secret `ExecutionContext`; every adapter/child consumes
those resolved bindings rather than re-reading ambient config independently.

### `ResourceClaim`

| Field | Type | Required | Invariant |
|---|---|---:|---|
| `resource_id` | stable string | yes | case-folded canonical path or named external target |
| `mode` | `shared` or `exclusive` | yes | mutation requires exclusive unless adapter proves otherwise |
| `scope` | `user_machine` | yes | coordinates profiles/checkouts for the same Windows user |

All claims for a workflow are acquired in deterministic sorted order before the
first mutating step, preventing cross-job deadlock and partial overlap. Path
identity resolves aliases/junctions where practical, and sync source/target
containment is rejected. Every datacli mutation consumer must use this manager;
the lock namespace is user-wide rather than profile-local. Credential caches
that may refresh/write are resources, identified by canonical path/safe hash,
never by token/key value. The guarantee does not cover external programs.

### `ExecutionPolicy`

| Field | Type | First-release value |
|---|---|---|
| `on_step_failure` | enum | `stop` |
| `same_job_overlap` | enum | `skip` in the runner; OS adapter must permit launch |
| `resource_overlap` | enum | `skip` |
| `lock_wait_seconds` | non-negative integer | `0` |
| `execution_timeout_seconds` | positive integer | runner-owned soft timeout; explicit per job/default |
| `retry` | structured policy | `none` until failure classes exist |
| `retain_runs` | positive integer/duration | explicit retention policy |

The overlap result is application-level. A backend policy that suppresses
runner startup cannot satisfy it. The policy limits concurrency; it does not
provide exactly-once execution or de-duplicate later manual/recovery attempts.

### `CommandResult`

| Field | Type | Invariant |
|---|---|---|
| `outcome` | `succeeded`, `no_op`, `failed`, `cancelled`, `timed_out` | typed domain result |
| `exit_code` | integer or null | raw process result when applicable |
| `failure_class` | optional enum/string | safe classification; not inferred from prose |
| `effect` | `none`, `complete`, `partial`, `unknown` | domain mutation already applied before return |
| `retry_guidance` | `safe`, `inspect`, `unsafe`, `unknown` | adapter evidence; never triggers an implicit retry |
| `summary` | optional redacted string | presentation only |
| `metrics` | optional non-secret map | adapter-defined structured counts |

Adapters construct this result. The runner does not parse stdout/stderr to
decide success or no-op.

### `StepResult`

| Field | Type | Invariant |
|---|---|---|
| `index` | positive integer | order in workflow |
| `command` | canonical display string | derived from `CommandSpec`; redacted |
| `started_at`, `finished_at` | UTC timestamps | ordered when step started |
| `outcome` | `succeeded`, `no_op`, `failed`, `cancelled`, `timed_out`, `not_run` | terminal |
| `exit_code` | integer or null | present when process started and exited |
| `error_class` | optional enum/string | never substitutes for exit code |
| `log_offset` | optional byte/range reference | points into immutable run log |

### `RunRecord`

| Field | Type | Invariant |
|---|---|---|
| `run_id`, `profile_id`, `job_id` | stable IDs | identify one runner invocation |
| `definition_digest` | hash | digest requested; exact `JobSpec` if execution begins |
| `definition_generation` | positive integer | generation named by runner action/request |
| `definition_snapshot_ref` | store reference/null | null only when snapshot loading itself failed |
| `snapshot_status` | `loaded`, `missing`, `mismatch` | explains whether execution had a definition |
| `dispatch_kind` | `backend`, `foreground_test`, `direct_runner` | fact known to the runner |
| `backend_cause_hint` | `calendar`, `run_now`, `unknown` or null | optional later backend observation, never invented |
| `scheduled_for_hint` | timestamp or null | optional best-effort backend evidence; not required |
| `observed_started_at`, `finished_at` | timestamps | actual runner observations; duration derivable |
| `outcome` | `succeeded`, `no_op`, `failed`, `environment_unavailable`, `skipped_overlap`, `stale_dispatch`, `invalid`, `cancelled`, `timed_out`, `abandoned` | terminal and decoded |
| `steps` | ordered `StepResult` list | later steps `not_run` after failure |
| `log_path` | absolute path | same-user readable; no secrets |
| `journal_path` | absolute path | isolated per-run event stream |
| `runtime_binding_fingerprint` | hash | safe observed binding identity |
| `runner_version` | version/commit identity | supports drift diagnosis |
| `command_contract_version` | version/hash | explains registry compatibility |

Run history is append-only and isolated per run so concurrent processes do not
share an unsafe append target. A rebuildable index supports listing. A
crash-recovery pass may append a terminal `abandoned` event; it does not rewrite
the original start event. If the runner cannot durably create its start event,
it exits before mutation. Retention is reference-aware and quota-aware.

Task Scheduler launches the same registered action for calendar and `/Run`
dispatches and does not pass a nominal occurrence timestamp to that action.
Consequently neither `backend_cause_hint` nor `scheduled_for_hint` is required
for a truthful `RunRecord`.

### `DispatchReceipt`

| Field | Type | Meaning |
|---|---|---|
| `requested_at` | UTC timestamp | when datacli asked the backend to run |
| `accepted` | boolean | backend accepted/rejected the request |
| `backend_token` | string/null | correlation identity only if backend supplies one |
| `raw_result` | string/integer/null | unmodified request result |
| `message` | string | safe decoded request status |

A dispatch receipt is not evidence that the runner started or completed.

### `BackendTaskState`

| Field | Type | Meaning |
|---|---|---|
| `exists` | boolean | OS task found at derived identity |
| `enabled` | boolean/null | actual backend state |
| `state` | backend-neutral enum plus raw value | ready/running/disabled/missing/unknown |
| `next_run_at`, `last_run_at` | timestamp/null | backend observation |
| `last_result_raw` | string/integer/null | unmodified OS result |
| `last_result_decoded` | string/null | datacli explanation when known |
| `installed_digest` | hash/null | definition digest embedded at install |
| `installed_generation` | integer/null | desired generation embedded at install |
| `missed_run_count` | integer/null | backend aggregate when available; not a per-occurrence ledger |
| `history_available` | boolean/unknown | whether detailed backend history can be queried |
| `drift` | list of findings | missing action/path/definition mismatch/etc. |

### `ReconciliationState`

| Field | Type | Meaning |
|---|---|---|
| `desired_digest`, `desired_generation` | hash/integer/null | current pointer or null tombstone |
| `backend` | `BackendTaskState` | independent OS observation |
| `execution` | latest run summary/null | independent datacli observation |
| `state` | enum | `in_sync`, `missing_task`, `stale_task`, `orphan_task`, `install_failed`, `delete_pending`, `incompatible`, `unknown` |
| `findings` | ordered list | actionable, evidence-bearing differences |

No plane is filled using assumptions from another. In particular, a successful
backend last result is not substituted for a datacli terminal run record.

## State machines

### Desired/backend reconciliation lifecycle

```text
JOB_DRAFT -> VALIDATED -> DESIRED ----reconcile----> IN_SYNC <-> PAUSED
                         |                         |
                         +----> DEGRADED <---------+
DESIRED/PAUSED -> TOMBSTONED ----reconcile----> REMOVED
```

- `JOB_DRAFT` may be incomplete and is neither runnable nor installable.
- `VALIDATED` means schema/static/non-interactive readiness checks passed at
  that moment; transient network reachability and paid work are not required.
- `DESIRED` is an atomically committed current pointer, not proof of OS install.
- `IN_SYNC` means a fresh backend query reports matching generation/digest and
  desired enabled state, or proves clean absence for a tombstone. A failed or
  ambiguous absence observation remains `unknown`.
- `DEGRADED` is explicit reconciliation drift/error, not automatic repair.
- `REMOVED` retains history and referenced snapshots unless separately purged.

### Run lifecycle

```text
RECEIVED -> SNAPSHOT_CHECK -> VALIDATION -> LOCKING -> RUNTIME_PREFLIGHT -> RUNNING
                  |              |            |               |              +--> SUCCEEDED | NO_OP | FAILED
                  |              |            |               +-----------------> ENVIRONMENT_UNAVAILABLE
                  |              |            +---------------------------------> SKIPPED_OVERLAP
                  |              +----------------------------------------------> INVALID
                  +-------------------------------------------------------------> STALE_DISPATCH
RUNNING -----------------------------------------------------------------------> CANCELLED | TIMED_OUT
orphaned non-terminal record --------------------------------------------------> ABANDONED
```

Every transition emits a journal event before or atomically with the externally
visible action it represents where practical. If Windows suppresses process
startup, there is no run lifecycle and no `RunRecord`.

### Runner ordering contract

1. Generate a run ID and durably create a minimal per-run start event/log naming
   the expected generation/digest; exit before mutation if this fails.
2. Load the exact immutable snapshot named by generation/digest.
3. Compare it with the active desired pointer/tombstone.
4. Re-resolve command contracts and non-secret runtime bindings without network
   or side effects; fail on drift and freeze them into one `ExecutionContext`.
5. Acquire the per-job lock, then sorted resource locks with OS-held ownership.
6. Recheck desired generation/digest after locking.
7. Run side-effect-free runtime preflight while holding locks. It may observe
   network/auth readiness but never performs paid work or refreshes/writes a
   credential cache.
8. Execute each step at most once in order; any credential refresh occurs under
   its declared resource lock. There is no implicit retry.
9. Flush a terminal event, release resources and update only rebuildable indexes.

An edit after step 6 affects the next run; the active run never changes
definition mid-flight.

## Ports

### `CommandRegistry`

- `list_capabilities() -> Capability[]`
- `validate(family, verb, argv, context) -> ValidatedCommand`
- `preflight(command, phase, context) -> Finding[]`, where phase is
  `static`, `readiness` or `runtime`
- `execute(command, execution_context) -> CommandResult`

It owns command admission, contract version, safe binding resolution and derived
resource claims. Every mutating consumer uses its lock contract. It does not
own timing. All preflight phases are domain-non-mutating; static/readiness make
no paid/live network call, and runtime never writes credential state. Adapters
must consume the supplied frozen bindings, not silently resolve mutable ambient
configuration again.

### `JobStore`

- `get_current(profile_id, job_id) -> desired pointer/tombstone`
- `get_snapshot(profile_id, job_id, generation, digest) -> JobSpec`
- `get_draft(...)`, `put_draft(...)`, `discard_draft(...)`
- `commit_draft(draft_id, expected_base_generation?) -> desired pointer`
- `list(profile_id) -> JobSpec[]`
- `commit_validated(spec, expected_generation?) -> desired pointer`
- `tombstone(profile_id, job_id, expected_generation?) -> desired pointer`
- `purge(..., explicit policy) -> purge report`

Snapshot writes and current-pointer compare-and-swap are atomic. Referenced
snapshots are immutable. The unresolved physical location is OQ-001.

### `SchedulerBackend`

- `install(spec, runner_action_with_generation_digest) -> BackendTaskState`
- `remove(profile_id, job_id) -> BackendTaskState`
- `enable(...)`, `disable(...)`
- `run_now(...) -> DispatchReceipt`
- `stop(...) -> cancellation request result`
- `query(...)`, `list(profile_id)`
- `observe_history(...) -> backend observations/unavailable`

It receives a complete runner action and trigger policy. It never interprets a
datacli workflow step. Backend query/history never creates or edits a
`RunRecord`.

On Windows, installed-definition observation uses Task Scheduler XML. ADR-005's
fixed read-only ScheduledTasks probe supplies locale-neutral runtime fields.
It also proves clean root-task absence when XML query returns non-zero. Failure
of either source remains explicit; localised `schtasks` prose is never parsed or
used to fill another state plane.

### `JobRunner`

- `preflight(spec) -> Finding[]`
- `execute(profile_id, job_id, expected_generation, expected_digest,
  dispatch_context) -> RunRecord`
- `cancel(run_id) -> cancellation result`

It owns step ordering, failure propagation, locks, logs and journal writes. It
does not calculate future trigger times or invent the cause/nominal time of an
opaque backend dispatch.

### `LockManager`

- use OS-held ownership, not deletion of a guessed-stale marker file;
- use a same-user machine-wide namespace so profiles/checkouts contend;
- acquire the per-job lock and record holder/run metadata for diagnostics;
- acquire sorted `ResourceClaim`s;
- report holder/run identity;
- release automatically on process death where the platform permits;
- provide the same acquisition path to interactive/headless mutations.

### `RunJournal`

- create one isolated per-run event stream and append immutable events;
- reconstruct one run;
- list recent runs by profile/job;
- identify abandoned non-terminal runs;
- rebuild disposable indexes from run streams;
- apply quota/reference-aware retention without mutating surviving records.

## Exit semantics

| Runner exit | Meaning |
|---:|---|
| 0 | workflow succeeded or completed as a typed no-op; record distinguishes |
| 1 | one or more started steps failed |
| 2 | invalid job, config or preflight |
| 3 | cancelled or timed out |
| 69 | runtime environment unavailable before mutation |
| 75 | skipped because an overlap policy denied the run |
| 76 | stale dispatch/definition generation; no mutation started |
| 78 | durable run journal could not be established; no mutation started |

Exact values may change before STABLE, but outcomes must remain distinct and
Windows status must decode them.

## Contract oracles

| Contract | Mechanical oracle |
|---|---|
| record schemas | JSON schema plus typed constructor tests |
| registry/inventory agreement | INV-002 parity test |
| no shell interpretation | subprocess argument-capture tests; `shell=False` invariant |
| stop-on-failure | state-machine tests showing later steps `not_run` |
| no overlap | concurrent scheduled and interactive tests across same job/resource |
| snapshot consistency | edit/delete/dispatch race and exact historical reconstruction tests |
| runtime binding safety | env/config/path/backend drift and alias/containment fixtures |
| append-only history | per-run concurrent event reconstruction, truncation, disk-full and crash recovery tests |
| adapter separation | backend contract tests using a fake backend and fake runner |
| no secrets | fixture scan of definitions, XML and logs |
| drift observability | path/digest/task mismatch fixtures reflected in status |
| honest dispatch | backend-suppressed launch and ambiguous run-now never fabricate a run/cause/time |
| cancellation | nested Windows process-tree stop test before strong cancellation wording |
| delivery semantics | reboot, duplicate dispatch, DST and multi-miss scenarios from INV-004 |
