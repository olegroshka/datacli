---
id: ADR-001
title: Put job meaning in a shared datacli execution substrate
status: STABLE
decision_state: ACCEPTED
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.0
sources:
  - KB-001
  - KB-002
  - Shared Substrate v0.2, sections 4-6
depends_on: [KB-001, KB-002, GLOSSARY]
referenced_by: [INV-001, INV-003, DD-001, ADR-002, ADR-003, ADR-004, OQ-001]
---

# ADR-001 - Put job meaning in a shared datacli execution substrate

## Approval warning

Accepted by the owner through the scheduler implementation authorization on
2026-08-17.

## Context

Datacli has several headless CLIs and one interactive shell, but no common job
definition, run journal, resource locking or scheduling boundary. A scheduler
implemented directly around Windows task actions could duplicate command
validation, quoting, execution and status semantics. A Python scheduler daemon
would add a new always-on runtime without removing that duplication.

The initiative needs a stable centre that survives replacement of the OS
scheduler, CLI presentation or individual command implementation.

## Proposed decision

Create a shared execution substrate owned by datacli with five responsibilities:

1. canonical command registry and validation;
2. infrastructure-neutral job/workflow records;
3. non-interactive workflow runner and same-user machine-wide resource locks
   shared by every datacli mutation path and profile;
4. append-only run journal and log contract;
5. scheduler backend port for install/query/lifecycle operations.

The interactive shell, headless schedule-management CLI, Windows adapter and
future platform adapters consume this substrate. They do not redefine job or
workflow semantics.

An installed OS task has one action: launch the datacli runner with a profile,
job ID, expected generation and definition digest. The operating system owns
trigger calculation and task lifecycle; the runner owns datacli command
meaning, ordering, validation, locking and outcome recording.

The existing CLIs may remain subprocess-backed adapters initially. Extraction
of domain callables is incremental, provided all execution passes through the
same registry, typed `CommandResult` and resource-lock contracts before the
corresponding no-overlap guarantee is declared stable. A runner freezes
validated non-secret runtime bindings into its execution context; a legacy
subprocess adapter must receive those bindings explicitly rather than re-read
mutable ambient configuration.

Desired job state, backend observation and execution history remain separate
state planes. ADR-004 proposes their reconciliation and snapshot protocol.

## Alternatives considered

### A. Direct Windows task per datacli command

Rejected in the proposal because command strings, environment, logging and
failure semantics would live in task definitions, making workflows and future
platform support divergent.

### B. Multiple Windows actions for workflow steps

Rejected because Task Scheduler would own step composition while datacli needs
fail-fast semantics, shared locks, one log and portable run history.

### C. APScheduler or another in-process Python scheduler

Rejected for the current desktop application because it requires a long-lived
process and persistent scheduler operations. It could become a future backend
for a datacli service without changing the proposed substrate.

### D. Keep each CLI independent and wrap them ad hoc

Rejected because dry-run enforcement, non-interactive policy, resources and
status would drift between command families.

## Consequences

### Positive

- One semantic path for foreground tests and scheduled runs.
- OS-specific code remains narrow and replaceable.
- Workflows, logs, locks and run results are portable.
- Existing CLIs can be adopted incrementally.
- Testing does not require installing real Windows tasks for most behaviour.

### Costs and risks

- A new internal package and versioned job schema are required.
- Some current CLIs need refactoring or explicit adapters.
- Direct interactive/headless mutations must adopt the shared locks; scheduler
  integration alone is insufficient.
- Definition state and OS state can drift and must be reconciled.
- The runner becomes load-bearing and needs crash-safe journalling.

## Acceptance criteria

- Human approval of the substrate/adapter boundary.
- DD-001 can express every scenario in KB-001 without Windows-only fields.
- A fake backend and fake commands can test an entire workflow.
- The Windows adapter can be removed without changing `JobSpec` or `RunRecord`.

## Decision outcome

Accepted as proposed on 2026-08-17. DD-001 and INV-003 were reconciled in the
same WP-00 change.
