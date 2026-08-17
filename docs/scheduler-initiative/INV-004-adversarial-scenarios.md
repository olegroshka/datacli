---
id: INV-004
title: Adversarial scheduler scenarios and failure modes
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - KB-001
  - KB-002
  - ADR-002
  - Microsoft Task Scheduler documentation, accessed 2026-08-17
depends_on: [KB-001, KB-002, ADR-002, ADR-004, ADR-005, GLOSSARY]
referenced_by: [INV-001, INV-003, OQ-002, OQ-003]
---

# Adversarial scheduler scenarios and failure modes

This inventory tries to falsify the proposed design. It is not a test backlog
of unlikely curiosities: every `CRITICAL` or `HIGH` row changes a contract or a
work package. A row is closed only when the stated behaviour and oracle exist.

## Corrected delivery claim

Windows Task Scheduler provides durable trigger registration, not exactly-once
delivery. A trigger can be delayed or suppressed before datacli starts, a
machine can reboot during execution, a manual run can duplicate scheduled work,
and a crash can occur after domain mutation but before a terminal journal event.

The defensible first-release contract is therefore:

- no more than one datacli execution holding the same job/resource lock at a
  time;
- one attempt per workflow step inside one runner invocation, with no implicit
  retry;
- best-effort trigger delivery and at-least-once *possibility*, not exactly-once
  execution;
- incremental/idempotent domain commands are required for safe recovery;
- status never equates an OS launch result with a completed datacli run.

## Scenario matrix

| ID | Severity | Adversarial scenario | Required behaviour | Oracle / disposition |
|---|---|---|---|---|
| AS-01 | CRITICAL | Task Scheduler applies `IgnoreNew` while a prior task instance runs. | Do not use `IgnoreNew` when datacli promises a `skipped_overlap` record: the runner would never start. Use OS `Parallel`, then let the runner journal and reject under an OS-held per-job lock. | Two simultaneous dispatches yield one execution and one durable `skipped_overlap` record. OQ-002. |
| AS-02 | CRITICAL | An interactive refresh starts while a scheduled sync or refresh is running. | All mutating registry consumers, including interactive/headless direct commands, acquire the same canonical resource locks. Until propagated, status/docs must say protection is scheduled-runner-only. | Cross-interface concurrency test. ADR-001/WP-01. |
| AS-03 | CRITICAL | Power, logon, network, disabled-task or OS policy prevents the runner process from launching. | A missing `RunRecord` is not success or a datacli-level skip. Status presents desired state, backend observation and runner history separately; backend history is best effort and explicitly unavailable when Windows history is disabled. | Suppressed-launch integration fixtures; no fabricated `RunRecord`. OQ-002. |
| AS-04 | HIGH | Job edit commits but OS task replacement fails, or the process crashes between the two writes. | The file store is authoritative desired state. Persist a generation and immutable definition snapshot, then reconcile the OS task. Report stale/missing/pending backend state; never claim a cross-system transaction. | Fault injection at each update phase. ADR-004. |
| AS-05 | HIGH | An old OS action launches during or after a job edit/delete. | Put expected definition digest/generation in the runner action. At startup compare it with the current desired pointer/tombstone and journal `stale_dispatch` without mutation. A runner already past that gate keeps its loaded immutable snapshot. | Edit/dispatch and delete/dispatch race tests. ADR-004. |
| AS-06 | HIGH | A run record contains only a digest, then the job is edited or deleted. | Retain content-addressed immutable `JobSpec` snapshots referenced by runs; retention must not remove a referenced snapshot. | Historical run reconstructs exact non-secret definition after edit/delete. ADR-004. |
| AS-07 | HIGH | `EODHD_DATA_ROOT`, `datacli.toml`, macro root, sync backend or destination changes after installation. | Persist non-secret resolved runtime bindings/fingerprints, re-resolve at execution and fail closed on material drift unless the user explicitly revalidates/reconciles. | Environment drift fixtures. DD-001/WP-02. |
| AS-08 | HIGH | Current-user task fires after logout, or an S4U task attempts Drive access. | Logged-on execution is explicit. S4U is not offered for network/Drive jobs; logged-out operation requires a separately approved Windows credential/service-account policy, with no password handled by datacli. | Principal/logon integration matrix. OQ-002. |
| AS-09 | HIGH | Trigger time falls in a DST gap/overlap, the Windows timezone changes, or the user travels. | Store local-wall-clock intent and timezone observed at install; use an offset-free Windows `StartBoundary` only with explicit Windows-local semantics. Show timezone drift and document/test DST behaviour rather than promising a nominal UTC instant. | Spring/fall and timezone-change integration tests. OQ-002. |
| AS-10 | HIGH | Laptop sleeps through several occurrences and `StartWhenAvailable` may later launch a delayed instance. | Do not claim replay of every missed occurrence or invent `scheduled_for`. Record observed runner start; expose backend missed-run information when available and label nominal time/cause as best effort. | Multi-day sleep fixture/manual test. DD-001/OQ-002. |
| AS-11 | HIGH | Two runner processes append one shared JSONL file, or disk fills mid-event. | Use isolated per-run journals with atomic event writes/flush, plus a rebuildable derived index. Refuse mutation if a run-start record cannot be made; terminal-record failure becomes an explicit degraded/error condition. | Concurrent writer, truncation and disk-full fault tests. WP-03. |
| AS-12 | HIGH | Task is stopped while a child fetcher/upload process is running. | Cancellation is not declared reliable until the implementation proves process-tree termination on Windows. `pause` affects future triggers only; `stop` is separate and reports whether descendants are confirmed stopped. | Nested-child cancellation integration test. WP-03/WP-04. |
| AS-13 | HIGH | Machine reboots or process crashes after an upload/mutation but before success is journalled. | Recover the record as `abandoned`; do not automatically repeat paid work in v1. User may inspect and run again. Commands admitted for mutation must be incremental/idempotent or document their recovery boundary. | Kill/reboot fault tests; command-specific recovery review. INV-002. |
| AS-14 | HIGH | `schedule run` is requested while the task is disabled or another dispatch occurs at the same instant. | `run` returns a backend `DispatchReceipt`, not a `RunRecord`. `--wait` may correlate a newly observed run by dispatch token/time window but must report ambiguity/timeout. Foreground `test` is the deterministic path. | Concurrent run-now/schedule test. DD-001/OQ-003. |
| AS-15 | HIGH | Delete is requested while a run is active. | Default delete refuses while active. `pause`/disable does not cancel it. Explicit `stop`, confirmation of terminal/unknown state, then delete/tombstone; history and referenced snapshots remain. | Active-delete lifecycle test. ADR-004/OQ-003. |
| AS-16 | MEDIUM | Code update changes the registry meaning while the stored argv and digest stay the same. | Store schema and command-contract version/fingerprint; revalidate on every run and report incompatible definitions. Record runner version in results. | Old-definition/new-registry compatibility fixtures. DD-001. |
| AS-17 | MEDIUM | Child output or an exception includes a token, signed URL or secret-like value. | Guarantee that definitions/actions contain no secrets. Apply known-value and pattern redaction to captured output, restrictive file permissions and leak-scan tests; do not claim arbitrary child output can be proven secret-free. | Canary-secret fixture scan. WP-03. |
| AS-18 | MEDIUM | Differently cased paths, junctions or source/destination containment evade resource locks or make local sync recursive. | Canonicalise Windows path identity, case-fold resource IDs, resolve aliases where possible and reject unsafe source/destination containment. Validate job IDs before deriving OS task names. | Alias/junction/containment and hostile-slug tests. DD-001. |
| AS-19 | MEDIUM | Existing CLI returns `0` for a legitimate no-op, or prints a misleading completed-step count after fail-fast. | Registry execution returns a typed `CommandResult` (`succeeded`, `no_op`, `failed`) rather than forcing the runner to parse prose. Fix misleading summaries before they become scheduled logs. | Adapter result tests and fail-fast summary regression. WP-01. |
| AS-20 | MEDIUM | Logs grow without bound or a retained run points to a removed definition/log. | Retention is explicit, reference-aware and quota-aware. Derived indexes are rebuildable; purge is separate from delete and reports what becomes unrecoverable. | Retention/reference integrity tests. WP-02/WP-03. |
| AS-21 | MEDIUM | `macro fetch -> sync push` appears to back up both datasets. | Make clear that current sync scans the EODHD data root only; the default macro root is a sibling and is not included. A separate backup capability is required before claiming macro backup. | Capability/help assertion. INV-002. |
| AS-22 | MEDIUM | `schtasks` output or task folders behave differently by Windows locale/version/permissions. | Use a datacli-owned task-name prefix without assuming folder creation. Do not parse localised prose for correctness. ADR-005 uses a fixed read-only ScheduledTasks JSON observation and preserves unknown on failure. Doctor checks permissions without self-elevation. | JSON/observer failure fixtures and the current logged-on-user real lifecycle pass, including clean absence with disabled history; clean-user/non-admin and non-English Windows hosts remain environment gates. ADR-002/ADR-005/WP-04. |
| AS-23 | HIGH | Two profiles/checkouts share one data root, remote target or OAuth token cache but use profile-local locks. | Resource locks live in a same-user machine-wide namespace keyed by canonical non-secret resource identity. Include credential caches that may refresh/write; never use a token/key value in a lock ID. Preflight cannot write the cache before locks. | Cross-profile data-root and concurrent Drive-token refresh tests. DD-001/WP-01. |
| AS-24 | HIGH | A laptop changes to battery during refresh/upload and Windows stops the task. | AC-only means “do not start on battery” by default, not “hard-stop active mutation on battery”. Keep `StopIfGoingOnBatteries=false`; active work finishes or is cancelled through the runner contract. | AC-to-battery integration test. OQ-002/WP-04. |
| AS-25 | HIGH | Task Scheduler execution limit hard-kills the runner before it records timeout or stops descendants. | Runner owns a soft timeout and controlled process-tree cancellation. Any OS execution limit is a longer safety cap; recovery marks the non-terminal run `abandoned` and does not auto-retry. | Soft-timeout and OS-hard-cap integration tests. DD-001/WP-03/WP-04. |
| AS-26 | HIGH | `schedule create` persists an empty workflow although `JobSpec.steps` must be non-empty, or a crash leaves a half-edited installed job. | Incomplete composition is a separate `JobDraft` that cannot be installed or executed. Finalise/apply performs one validation and desired-generation commit; abandoned drafts are inspectable/discardable. | Draft/finalise/crash tests. DD-001/OQ-003/WP-02. |
| AS-27 | MEDIUM | Schedule creation occurs while offline and “full preflight” either fails forever or makes a paid/network call. | Separate static validation, non-interactive readiness and runtime/live preflight. Install performs no paid work and need not prove transient network reachability; execution records runtime unavailability. | Offline create/enable test with zero network calls. DD-001/WP-01. |
| AS-28 | HIGH | A command uploads/fetches some items, then fails; the run is labelled simply failed and appears to have done nothing. | `CommandResult` records effect as `none`, `complete`, `partial` or `unknown` plus retry guidance. Workflow stops, does not roll back successful domain work and never auto-retries v1 paid work. | Partial refresh/push failure fixtures. INV-002/DD-001/WP-01. |
| AS-29 | HIGH | The runner validates one data root/backend, then a legacy subprocess re-reads changed ambient config and mutates another target. | Build a frozen non-secret `ExecutionContext` after binding validation and pass it to every adapter/child; adapters may not independently re-resolve mutable config. Datacli config writes use an exclusive config resource lock; external/manual edits are detected where possible and remain outside the lock guarantee. | Config-change-between-steps and child-env capture tests. ADR-001/DD-001/WP-01. |

## Remaining adversarial questions

1. What Windows event-log observations are dependable enough for status when
   task history is enabled, and what exact `unknown` state appears otherwise?
2. What does Windows do for the repeated local time at the autumn DST boundary
   on each supported release?
3. Can `schtasks.exe` provide locale-neutral runtime state sufficient for the
   CLI, or must the adapter use a fixed PowerShell/CIM or COM query path?
4. What process-tree primitive will make `schedule stop` honest for nested
   Python subprocesses?

These questions are implementation spikes or OQ-002 policy inputs. None may be
silently answered by optimistic status text.
