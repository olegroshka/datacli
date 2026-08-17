---
id: INV-003
title: Scheduler implementation work packages
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.3
sources:
  - KB-001
  - KB-002
  - INV-002
  - DD-001
depends_on: [KB-001, KB-002, INV-002, INV-004, DD-001, ADR-001, ADR-002, ADR-003, ADR-004, ADR-005, OQ-001, OQ-002, OQ-003, GLOSSARY]
referenced_by: [INV-001]
---

# Scheduler implementation work packages

This is the forward-expansion inventory. It deliberately describes technical
work without implementing it. WP-01 may begin only after the four ADRs are
accepted or superseded, the three OQs are resolved, critical adversarial rows
in INV-004 have a chosen disposition, and downstream contracts are reconciled.

## Dependency order

```text
WP-00 review gate
   -> WP-01 command substrate and shared mutation locks
   -> WP-02 desired-state store and immutable snapshots
   -> WP-03 runner, locks and journal
   -> WP-04 Windows adapter
   -> WP-05 schedule CLI and shell integration
   -> WP-06 end-to-end verification and operating docs
```

WP-04 can begin after WP-02's serialisation contract stabilises and in parallel
with the latter part of WP-03. WP-05 depends on both.

## Work-package inventory

### WP-00 - human substrate review

- Inputs: KB-001, INV-004, ADR-001..004, OQ-001..003, DD-001.
- Output: accepted/superseding ADRs, resolved OQs, stable reconciled contracts.
- Oracle: INV-001 contains no unapproved choice presented as binding.
- Status: complete; owner approval recorded 2026-08-17.

### WP-01 - canonical command substrate

- Create a command registry with family/verb identity, argument validation,
  versioned contracts, safe runtime bindings, typed `CommandResult`, preflight,
  canonical resource declarations and non-interactive execution adapters.
- Separate static validation, no-network readiness and runtime/live preflight;
  no schedule-management operation performs paid work.
- Route the scheduler through that registry. Route every direct interactive and
  headless mutating command through the same resource-lock path before claiming
  global overlap protection.
- Put canonical resource locks in a same-user machine-wide namespace so
  different profiles/checkouts sharing a data root, target or writable OAuth
  cache contend correctly.
- Add `DATACLI_NONINTERACTIVE=1` or an equivalent explicit execution context.
- Make Google Drive authentication fail with a clear instruction instead of
  opening a browser in scheduled mode.
- Fix misleading fail-fast completion counts and distinguish typed no-op from
  performed work without parsing output. Report partial/unknown effects and
  retry guidance for failed or cancelled CORE commands.
- Canonicalise Windows path/resource identity and reject sync source/target
  containment. Document that current EODHD-root sync does not include macro's
  sibling root.
- Freeze validated bindings into one execution context and make legacy child
  processes consume explicit values. Add shared/exclusive config resource locks
  for datacli readers/writers; do not let steps re-resolve a changed target.
- Candidate modules: `scheduler/commands.py`, small adapters near existing CLIs.
- Status: implemented and verified by focused registry/direct-lock tests.
- Oracle: INV-002 parity tests; every CORE command parses and dry-runs headlessly;
  every FORBID command is rejected; scheduled and interactive conflicting
  mutations contend on one lock; adapters return typed results.

### WP-02 - desired-state model, serialisation and snapshot store

- Implement DD-001 records with a versioned JSON representation.
- Resolve profile identity and storage from OQ-001.
- Implement immutable content-addressed snapshots, atomic generation pointers,
  tombstones, same-user ACLs and reference-aware retention.
- Implement non-executable drafts, base-generation conflict detection and one
  finalise/apply commit so incomplete workflows are never desired state.
- Commit desired state independently, then expose backend reconciliation rather
  than claiming a file/Task-Scheduler transaction.
- Persist and revalidate non-secret repo, interpreter, config, data-root,
  command-contract and sync-target bindings.
- Candidate modules: `scheduler/model.py`, `scheduler/store.py`.
- Status: implemented with `scheduler/schema/job-spec-v1.schema.json` and
  verified by schema/store/CAS/snapshot tests.
- Oracle: schema round-trip, migration/version rejection, atomic-write,
  generation-CAS, tombstone, retention/reference and drift tests; historical
  runs resolve exact snapshots; no secrets in serialised fixtures.

### WP-03 - workflow runner, locks and run journal

- Execute ordered steps with stop-on-failure semantics.
- Durably create an isolated per-run journal before mutation.
- Acquire an OS-held per-job lock and derived cross-job resource locks with
  zero-wait skip behaviour and diagnostic holder metadata.
- Load/check exact expected generation/digest, recheck it after locking and
  journal stale dispatches without mutation.
- Capture stdout/stderr, timings, step exit codes and final run outcome.
- Produce redacted human logs plus per-run append-only structured history and a
  rebuildable index; handle truncation, concurrent starts, retention and disk
  exhaustion explicitly.
- Support foreground `test` and the same headless `run <job-id>` used by the OS.
- Prove Windows child-process-tree cancellation before reporting a strong
  terminal `cancelled` outcome; otherwise report cancellation requested/unknown.
- Implement runner-owned soft timeout through the same process-tree mechanism;
  an OS hard cap remains a longer last resort recovered as `abandoned`.
- Candidate modules: `scheduler/runner.py`, `scheduler/locks.py`,
  `scheduler/journal.py`.
- Status: implemented and verified with failure, overlap, stale-dispatch,
  redaction, abandoned-recovery, journal-failure and Windows Job Object timeout
  fixtures. Strong `/End` cancellation remains deliberately unclaimed.
- Oracle: state-machine, failure propagation, stale-dispatch, nested-child
  cancellation, scheduled/interactive overlap, crash, disk-full, concurrent
  journal and canary-secret redaction tests.

### WP-04 - Windows Task Scheduler adapter

- Implement the `SchedulerBackend` port from DD-001.
- Generate Task Scheduler 2.0 XML and register/query/run/stop/remove through
  argument-array `schtasks.exe` calls.
- Install one action that launches the datacli runner with an absolute
  interpreter, explicit working directory, job ID, generation and digest.
- Use a validated root task-name prefix unless task-folder creation is explicitly
  provisioned/tested. Use `Parallel`; runner locks decide overlap.
- Treat run-now as a `DispatchReceipt`; keep desired, backend and runner state
  separate. Expose unavailable Task Scheduler history as unknown.
- Reconcile installed task state with desired generation/digest and decode only
  results whose source/meaning is known.
- Secure/delete temp XML and check permission without self-elevation.
- Candidate modules: `scheduler/backends/base.py`,
  `scheduler/backends/windows.py`.
- Status: implemented. ADR-005 records the locale-neutral observation revision;
  XML/argument/fake-observer tests pass. The authorised harmless real task was
  registered, observed before/after dispatch, correlated to a successful
  journal record, deleted and proven absent on 2026-08-17.
- Oracle: XML golden tests, stale-action and query fixtures, no-shell invocation
  tests and a harmless prefixed Windows integration task. Matrix includes
  non-admin user, non-English locale, disabled history, logout/lock, sleep,
  AC-to-battery transition, DST, simultaneous dispatch, soft timeout, OS hard
  cap and nested-child `/End`. If locale-neutral state cannot be obtained from
  `schtasks`, revisit ADR-002 before proceeding.

### WP-05 - management CLI and cmd2 integration

- Implement `schedule commands/list/show/status/add/create/step/test/run/pause/
  resume/stop/edit/delete/reconcile/purge/doctor` after OQ-003 fixes the exact
  first-release grammar.
- Expose the same headless management CLI outside `cmdloop()`.
- Render desired, Windows and run-journal state as separate planes plus an
  evidence-bearing reconciliation result.
- Make status/doctor read-only; reconcile explicit. Refuse active delete, make
  pause future-only and make run-now acceptance distinct from completion.
- Candidate modules: `scheduler/cli.py`, a thin `do_schedule` delegate in
  `datacli.py`.
- Status: implemented and verified through the documented draft lifecycle with
  a fake backend. Every operation has contextual behavior/safety/default/example
  help. Completion covers parser operations/options, dynamic state identifiers,
  step actions/indexes, paths and all registry families/verbs/arguments from
  shared metadata.
- Oracle: rendered-help tests, parser/completion/registry parity tests,
  contextual cmd2 completion tests and management lifecycle integration.

### WP-06 - end-to-end verification and operation

- Exercise SC-01..SC-06 from KB-001.
- Verify same-user credential resolution without exporting credentials.
- Execute every INV-004 scenario classified CRITICAL/HIGH and record the exact
  behaviour of supported Windows releases.
- Verify missed-run/history-unavailable, disabled, overlap, timezone/DST,
  moved-repository, changed environment/config and missing-venv status reporting.
- Verify reboot/kill recovery marks abandoned work without automatically
  repeating paid operations.
- Add user documentation and upgrade/remove guidance.
- Status: automated verification is complete: the scheduler suite passes 33
  tests and the full repository suite passes 425 tests on Windows using
  repository-local temporary roots. The authorised real-task lifecycle passed
  with one read-only, no-network command and secret-free single-action XML. No
  paid/network/credential/Drive effect was used. Disruptive environment matrices
  (logout/lock, sleep, battery, timezone/DST and non-English hosts) remain
  explicit-effect gates.
- Oracle: black-box Windows run plus automated acceptance suite; source and
  generated task XML inspected for secrets.

## Completion definition

The initiative is not complete when a Windows task appears in the GUI. It is
complete when the shared substrate expands into an implementation that passes
the listed oracles, never claims more dispatch/cancellation certainty than its
evidence, returns durable validated state to the run journal, and can be
reconstructed from stable artefacts without relying on this conversation.
