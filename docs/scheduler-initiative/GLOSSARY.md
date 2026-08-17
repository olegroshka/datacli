---
id: GLOSSARY
title: Scheduler initiative glossary
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.0
sources:
  - Shared Substrate v0.2
  - KB-001
  - DD-001
depends_on: []
referenced_by: [BOOTSTRAP-SCHEDULER, INV-001, KB-001, KB-002, INV-002, INV-003, INV-004, DD-001, ADR-001, ADR-002, ADR-003, ADR-004, ADR-005, OQ-001, OQ-002, OQ-003]
---

# Scheduler initiative glossary

This file is the semantic authority. Other artefacts cite these terms rather
than introducing local variants.

## Terms

**Backend task state**

The current state observed from an OS scheduler: existence, enabled/running
state, next and last run, raw last result and installed definition digest. It is
not a substitute for a datacli run record.

**Canonical command**

A datacli operation identified by a registered family and verb plus literal
arguments, such as `eodhd refresh --fast --run`. It is independent of an
interactive shell's current source or shortcut spelling.

**Command adapter**

The component that validates, preflights and executes one canonical datacli
command through its owning domain/CLI surface.

**Command family**

A stable namespace for related commands: initially `eodhd`, `macro`, `sync`,
and potentially `score`.

**Definition digest**

A deterministic hash of the validated persisted `JobSpec`. It links an
installed OS task and a `RunRecord` to an immutable definition snapshot.

**Definition snapshot**

An immutable, content-addressed serialisation of one validated `JobSpec`
generation. It is retained while any retained run references it.

**Definition drift**

A mismatch between desired datacli job state and installed/runtime state, such
as a missing task, changed digest, moved repository or missing interpreter.

**Desired state**

The authoritative profile-local current generation/digest (or tombstone) that
states what job datacli intends the backend to have installed.

**Dispatch receipt**

Evidence that a backend accepted or rejected a run-now request. It is not
evidence that the runner started or the workflow completed.

**Dispatch suppression**

A backend decision or environmental condition that prevents the runner process
from starting. It can have backend evidence but cannot have a datacli
`RunRecord` because no runner existed to write one.

**Dry-run command**

A command invocation that deliberately plans without applying its domain
mutation. Several existing commands require their own `--run` argument to stop
being dry-runs.

**Exactly-once**

A delivery guarantee this initiative explicitly does not claim. Locks prevent
concurrent datacli mutation, but crashes, missed triggers and later dispatches
can still produce no attempt or a repeated attempt.

**Foreground test**

Execution of a stored job immediately in the current terminal through the same
runner used by scheduled execution, without asking the OS scheduler to trigger
it.

**Headless**

Callable without `cmdloop()`, terminal input, browser interaction or human
prompts, with a reliable typed result.

**Job**

The durable datacli definition containing identity, trigger, ordered workflow
and execution policy. In CLI text, “schedule” may be used conversationally, but
`JobSpec` is the canonical domain record.

**Job runner**

The infrastructure-neutral process that loads an exact definition snapshot,
performs preflight and locks, executes workflow steps, writes logs/journal
events and returns a decoded exit result. It does not calculate future run
times.

**No-op**

A successful command result that performed no domain work because nothing was
required. It is distinct from failure and from an unattempted step.

**OS task**

The Windows Task Scheduler registration that owns trigger and native lifecycle
state and launches one datacli job runner action. It does not contain workflow
meaning.

**Oracle**

The strongest available mechanism that checks a layer against intent: human
review at the charter/decision layer, schemas and tests at contract/code layers,
and observed task/run state operationally.

**Profile**

A stable machine-local identity binding a datacli checkout/config context to
its job definitions and run history. Its physical derivation is open in OQ-001.

**Reconciliation**

An explicit comparison/repair operation between desired state and backend
state. It does not merge backend history into datacli execution history.

**Resource claim**

A registry-derived shared or exclusive access declaration for a canonical
resource such as the EODHD data root, macro root, sync manifest or remote target.

**Run journal**

Per-run append-only structured events from job and step execution. It is
distinct from the human-readable log and from Windows' last task result.

**Run now**

Ask the installed OS scheduler to launch a job immediately using its registered
principal and action. It returns a dispatch receipt and differs from a
foreground test.

**Schedule**

The association of a job with a trigger and enabled state. The CLI command group
is named `schedule`; avoid using “scheduler” for a particular job.

**Scheduler backend**

An adapter that maps infrastructure-neutral trigger and lifecycle operations to
an operating system or scheduling service. The first proposed backend is
Windows Task Scheduler.

**Shared execution substrate**

The common datacli layer containing command admission, job/workflow contracts,
runner semantics, locks, run history and the backend port. Interfaces and OS
adapters compose it; they do not redefine it.

**State planes**

The three independent views used by status: desired job state, observed backend
state and datacli execution history. Unknown data in one plane is not inferred
from another.

**Step**

One `CommandSpec` at a fixed position in a workflow.

**Trigger**

The rule determining when an OS scheduler starts a job, such as daily at 06:00
or weekly on Sunday. It does not describe what datacli does.

**Workflow**

A non-empty ordered sequence of canonical command steps executed by one runner
under one policy and run record. A one-command job is a one-step workflow.
