---
id: ADR-003
title: Represent scheduled work as allowlisted argv commands in fail-fast workflows
status: STABLE
decision_state: ACCEPTED
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.0
sources:
  - KB-001
  - KB-002
  - INV-002
depends_on: [KB-001, KB-002, INV-002, ADR-001, GLOSSARY]
referenced_by: [INV-001, INV-003, DD-001, OQ-003]
---

# ADR-003 - Represent scheduled work as allowlisted argv commands in fail-fast workflows

## Approval warning

Accepted by the owner through the scheduler implementation authorization on
2026-08-17, including OQ-003's recommended command set and workflow UX.

## Context

The user wants the scheduler command to take supported datacli commands and
arguments. Raw shell strings would make this superficially flexible, but would
also introduce quoting differences, injection risk, unbounded resource access,
secret leakage and commands whose interactivity or exit semantics datacli
cannot validate.

The normal operation is also multi-step: refresh local data, rebuild the index,
then push through the configured sync backend. Time-separating three independent
OS tasks cannot guarantee that earlier work finished successfully.

## Proposed decision

- A scheduled step is a typed `CommandSpec`: command family, verb and literal
  argv list.
- Only commands admitted by INV-002 and implemented by the canonical registry
  can be installed.
- Commands are never reconstructed into a shell string.
- A job contains a non-empty ordered list of steps. One command is a one-step
  workflow rather than a special case.
- Default workflow semantics are sequential and stop-on-first-failure.
- The registry revalidates each command and derives resource claims at runtime.
- Every mutating registry consumer acquires those same canonical claims; a
  scheduler-only lock does not satisfy the contract.
- Registry execution returns a typed result that distinguishes success, no-op
  and failure without parsing display text.
- Mutating dry-run-by-default commands must include their own `--run` opt-in.
- Interactive commands and secret-bearing arguments are rejected.
- Resource paths/targets are canonicalised and unsafe source/destination
  containment is rejected.
- No workflow has implicit retries or exactly-once semantics across separate
  runner invocations.
- The first UX uses `--` to delimit one command from schedule options. Multi-step
  composition uses explicit step-management commands; OQ-003 fixes the exact
  surface.

## Alternatives considered

### A. Arbitrary command line or shell script

Rejected because datacli cannot provide safety, resources, preflight, redaction
or stable semantics for arbitrary programs.

### B. Store one raw command string and parse it at execution

Rejected because quoting would be platform- and shell-dependent and definition
validation would not match execution.

### C. One OS task per step with time offsets

Rejected because durations vary and failure propagation becomes accidental.

### D. Hard-code named routines only

Rejected as the only interface because it hides the exact commands and prevents
legitimate supported compositions. Presets may expand transparently into normal
steps later.

## Consequences

### Positive

- Clear validation and security boundary.
- Deterministic quoting and portable persisted definitions.
- Exact logs and resource locking per known operation.
- Workflows express dependency rather than relying on wall-clock guesses.
- Presets can remain syntactic sugar over inspectable steps.

### Costs and risks

- Adding a newly schedulable command requires registry and inventory changes.
- Some existing parsers may need refactoring for validation without execution.
- Users cannot schedule arbitrary maintenance commands through this feature.
- The step-editing UX needs care to stay simple.

## Acceptance criteria

- INV-002 and the registry agree mechanically.
- Shell metacharacters remain literal argv data in tests.
- Unsupported and interactive commands fail before task registration.
- A failed first step leaves every later step `not_run` in `RunRecord`.
- `refresh -> reindex -> sync push` can be represented without a special case.
- Interactive and scheduled attempts against one canonical resource contend on
  the same lock in tests.

## Decision outcome

Accepted on 2026-08-17. OQ-003 is resolved to the recommended explicit draft
workflow and admitted-command set.
