---
id: KB-001
title: Scheduler initiative charter
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.0
sources:
  - Human intent stated in datacli scheduler discussion, 2026-08-17
  - Shared Substrate v0.2, sections 4-6
depends_on: [GLOSSARY]
referenced_by: [INV-001, KB-002, INV-002, INV-003, INV-004, DD-001, ADR-001, ADR-003, ADR-004, OQ-002, OQ-003]
---

# Scheduler initiative charter

## Intent

Add a clear, dependable scheduling capability to datacli so a user can run
supported datacli operations regularly without keeping the interactive shell
open. The same capability must support:

- refreshing data into the configured local data root;
- rebuilding local indexes after refresh;
- pushing the data root through the configured sync backend, whether Google
  Drive or a local destination;
- composing those operations into an ordered workflow;
- creating, inspecting, testing, running, pausing, resuming and deleting
  schedules from datacli.

The durable product is not a Windows command generator. It is a shared job
and execution model whose first timing adapter is Windows Task Scheduler.

## Users and operating context

- Primary context: a single user running datacli on Windows.
- The user already has a repository checkout, a managed `.venv`, local config,
  API credentials and, for Google Drive, a cached OAuth token.
- Jobs may be long-running and network-dependent.
- The computer may be asleep, on battery, disconnected, locked or logged out
  at the nominal trigger time.
- Datacli remains useful interactively; scheduling is an additional interface,
  not a replacement.

## Behavioural scenarios

### SC-01 - refresh local data only

Given a valid EODHD configuration and API credentials, when the user schedules
`eodhd refresh --fast --run`, the job refreshes the configured local data root,
records its result and does not perform a backup unless a later workflow step
requests one.

### SC-02 - refresh, index and back up

Given a three-step workflow of refresh, reindex and sync push, when the trigger
starts a runner, each step is attempted at most once within that run and in
order. A failed step stops later steps by default. A Drive push therefore cannot
race ahead of an unfinished or failed refresh inside that workflow. A later
manual or recovered run is a distinct attempt; the product does not promise
exactly-once delivery across crashes or trigger duplication.

### SC-03 - use the configured sync backend

Given `[sync].backend = "gdrive"` or `"local"`, the canonical scheduled command
`sync push --run` uses that configured backend. Scheduling does not create a
second backup configuration model.

### SC-04 - manage schedules from datacli

The user can list and show schedules, see enabled state, next and last run,
decoded result, current runner state and the latest log; can test a job in the
foreground; can request a run now; and can pause, resume, reconcile, stop or
delete it. `pause` prevents future dispatches and never implies cancellation of
an active run.

### SC-05 - recover from a missed trigger

If Windows cannot run a time-based job at its nominal time, the configured
missed-run policy and the strongest available backend observation are visible.
If the runner never starts, datacli must not fabricate a run record or silently
pretend that the scheduled work happened. When Windows history is unavailable,
status says that dispatch history is unknown.

### SC-06 - refuse unsafe or interactive commands

An unsupported command, arbitrary shell fragment, embedded secret or an
interactive command such as `sync login` is rejected during schedule creation,
before a Windows task is installed.

### SC-07 - edit and delete under race

If an old Windows task action starts while a job is being edited or deleted, it
must not silently execute the newest definition or continue an obsolete one.
It records a stale dispatch and fails before domain mutation. An already-running
job keeps the immutable definition it began with; delete refuses by default
until that run is terminal.

## Product invariants

1. **One execution meaning.** Interactive and scheduled use of a canonical
   command must reach the same domain operation and validation rules.
2. **OS scheduler owns when; datacli owns what.** Windows stores triggers and
   task lifecycle. Datacli stores workflow meaning and run history.
3. **No arbitrary shell.** Scheduled work is a typed, allowlisted argv command,
   never a user-controlled shell string.
4. **No interactive scheduled execution.** Scheduled jobs never open a browser,
   prompt for input or wait for a terminal.
5. **No secrets in definitions or actions.** OAuth tokens, API keys and Windows
   passwords are never copied into job JSON or Task Scheduler XML/action
   arguments. Captured child output is redacted and leak-tested, but arbitrary
   third-party output is not assumed safe without verification.
6. **No unsafe datacli overlap.** Every datacli mutation path that touches a
   canonical resource, interactive or scheduled and across profiles/checkouts,
   uses the same user-wide application lock. The OS permits the runner to launch
   so a rejected overlap can be journalled. External programs remain outside
   this guarantee.
7. **Honest observability.** Every runner that can establish its journal records
   structured step results, observed timestamps, decoded outcome and a durable
   log. Journal-establishment failure exits before mutation. A trigger suppressed
   before runner startup is a backend observation, never an invented run.
   Unknown history remains unknown.
8. **Fail closed.** Invalid definitions, missing runtime prerequisites and
   unknown command versions fail before a mutating step starts where possible.
9. **Explicit drift.** A missing OS task, moved repository, missing interpreter
   or mismatch between desired definition, runtime bindings and installed task
   appears in status and fails closed before mutation.
10. **Reversible management.** Pause/resume and deletion are explicit. Deleting
    a schedule retains run history and referenced definition snapshots unless
    the user separately requests purge. Delete does not imply stop.
11. **No exactly-once fiction.** Trigger delivery and crash recovery may produce
    a missed, delayed or repeated attempt. Commands admitted for mutation must
    be incremental/idempotent at their documented recovery boundary.
12. **Three-plane truth.** Desired job state, observed backend state and datacli
    execution history are separately visible and reconciled; none substitutes
    for another.

## Scope for the first working release

- Windows Task Scheduler as the only timing backend.
- Daily and weekly calendar triggers, plus run-now and foreground test.
- One or more ordered canonical command steps.
- Stop-on-failure workflow semantics.
- Per-job and per-resource overlap protection.
- File-backed desired state, immutable definition snapshots and structured
  per-run history.
- CLI management through the datacli shell and a headless entry point.
- Commands admitted by INV-002 after OQ-003 is resolved.

## Explicit non-goals

- A continuously running Python scheduler service.
- Distributed or remote scheduling.
- Arbitrary PowerShell, `cmd.exe`, Python snippets or external programs.
- A graphical scheduler UI.
- Cloud orchestration, queues or multi-machine failover.
- Capturing API keys or Windows account passwords.
- Replacing Task Scheduler's trigger engine or guaranteeing its event history.
- Graph-native substrate tooling for this initiative.

## Success oracle

The human oracle approves this charter. Below that layer, the first release is
acceptable when all seven scenarios have automated acceptance coverage where
mechanically possible, Windows adapter integration tests pass on Windows, a
real harmless scheduled test produces an observable run record, and no secret
appears in exported task XML or logs.
