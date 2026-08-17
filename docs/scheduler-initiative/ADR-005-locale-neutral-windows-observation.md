---
id: ADR-005
title: Use a fixed read-only PowerShell probe for locale-neutral Windows task observations
status: STABLE
decision_state: ACCEPTED
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.1
sources:
  - KB-002
  - ADR-002
  - scheduler/backends/windows.py
depends_on: [KB-002, ADR-002, GLOSSARY]
referenced_by: [INV-001, INV-003, INV-004, DD-001]
---

# ADR-005 - Locale-neutral Windows task observations

## Context

The ADR-002 implementation spike confirmed that `schtasks.exe /Query /XML`
returns the installed definition but not current state, next/last run, last
result or missed-run count. Its verbose tabular output localises both headers
and values. Parsing that prose would violate the accepted locale-neutral status
contract. ADR-002 explicitly required the boundary to be revised when this gap
was discovered.

## Decision

Keep generated Task Scheduler 2.0 XML and argument-array `schtasks.exe` calls as
the only mutation path. Add one narrow, fixed, read-only observation probe using
the built-in Windows PowerShell ScheduledTasks cmdlets:

- the script text is a source constant, not generated from user input;
- the validated task name is supplied through an environment variable;
- `powershell.exe` is launched with `-NoProfile -NonInteractive` and
  `shell=False`;
- output is a small JSON object containing stable property names for state,
  enabled, next/last run, last result, missed count and operational-log enabled
  state;
- `SCHED_S_TASK_HAS_NOT_RUN` (`0x41303`) is authoritative for a never-run task;
  the observer suppresses the meaningless placeholder `LastRunTime` that some
  Windows builds return with it;
- a fixed root-task enumeration distinguishes clean absence from an ambiguous
  non-zero `schtasks /Query` result; if that observation itself fails, backend
  existence and tombstone reconciliation remain unknown rather than being
  reported in sync;
- probe failure leaves runtime/history observations `unknown` and adds an
  evidence-bearing drift finding; it never falls back to localised prose;
- desired state and datacli run history remain independent of this probe.

This is accepted as the implementation-authorised revision path required by
ADR-002. It does not authorise PowerShell mutations, task-folder provisioning,
self-elevation or arbitrary script construction.

## Consequences

Windows PowerShell becomes a read-only observation dependency for full status.
Definition reconciliation remains inspectable XML plus `schtasks.exe`. Systems
without the ScheduledTasks module still manage definitions, but current runtime
fields honestly report unavailable/unknown.

## Oracle

- argument-capture tests prove no shell invocation and fixed script text;
- JSON fixtures are locale-independent;
- observer failure produces unknown fields rather than inferred state;
- no observer path registers, changes, runs, stops or deletes a task.
- the 2026-08-17 real fixture observed `0x41303` without a phantom last-run
  timestamp before dispatch, result `0` after dispatch, and clean absence after
  deletion while Task Scheduler operational history was disabled.
