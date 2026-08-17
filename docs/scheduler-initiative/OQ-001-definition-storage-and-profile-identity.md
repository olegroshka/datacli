---
id: OQ-001
title: Where should job definitions live and how is a datacli profile identified?
status: STABLE
question_state: RESOLVED
owner: Oleg Roshka
last_reviewed: 2026-08-17
target_resolution: 2026-08-24
version: 1.0
sources:
  - KB-002
  - ADR-001
  - DD-001
depends_on: [KB-002, ADR-001, ADR-004, DD-001, GLOSSARY]
referenced_by: [INV-001, INV-003]
---

# OQ-001 - Where should job definitions live and how is a datacli profile identified?

## Why this remains open

Definitions are machine/user operational state, but they point at a particular
repository, interpreter and config. Storing them in the repository makes them
easy to discover but couples version-controlled source to machine-local task
state. Storing them only in Windows task XML makes the OS task a duplicate
semantic source of truth.

## Options

### A. `%LOCALAPPDATA%\datacli\profiles\<profile_id>\` - recommended

- Keep an atomic desired pointer/tombstone per job plus immutable,
  content-addressed JSON `JobSpec` snapshots in a per-user, machine-local store.
- Store `repo_root`, interpreter and config identity in the profile record.
- Derive `profile_id` from an explicit generated UUID with a human label, not
  solely from a mutable path.
- Keep isolated per-run journals/logs and a rebuildable index under the same
  profile tree, protected by same-user ACLs.
- Store only the profile/job IDs, desired generation and definition digest in
  the Task Scheduler action/metadata.

Advantages: clean source-of-truth split, exact historical reconstruction,
works when launched from any directory, and avoids committing machine paths.
Cost: requires profile discovery, snapshot retention and an export/import story.

### B. Repository-local `.datacli/schedules/`

Advantages: definitions travel with the checkout and are easy to inspect.
Costs: machine paths and operational enabled state do not travel cleanly;
multiple clones collide unless identity is added; repository removal can remove
definitions while leaving installed Windows tasks.

### C. `datacli.toml`

Advantages: existing configuration location.
Costs: the current writer supports simple flat string-valued sections and is a
poor match for versioned nested workflows/history. It also conflates product
configuration with operational task state.

### D. Windows Task Scheduler XML as the only store

Advantages: one physical object.
Costs: makes Windows own datacli workflow meaning, harms portability and makes
history/schema migration difficult. In tension with ADR-001.

## Resolution criteria

- One authoritative `JobSpec` representation.
- Exact non-secret definition snapshot remains available for every retained
  run, even after edit/delete.
- No secrets or passwords in the store.
- Multiple checkouts and data profiles can coexist.
- Repository/interpreter movement is detected, not silently followed.
- Atomic updates and schema migration are testable.
- Cross-store/OS partial failure is exposed through reconciliation rather than
  described as atomic.
- Export/import is possible without exporting secrets.
- Task deletion and profile deletion have explicit, recoverable semantics.

## Recommended resolution

Adopt option A plus ADR-004. Create profiles explicitly on first schedule
creation, give them a generated stable ID and display label, and provide
`schedule profile`, `schedule export` and later `schedule import` operations.
Treat paths/runtime bindings as observed properties whose drift is reported and
requires explicit revalidation. Deletion writes a tombstone and preserves run
history/snapshots; purge is separate and reference-aware.

## Resolution output

Resolved by owner authorization on 2026-08-17: option A is accepted through
ADR-004. DD-001 and INV-003 are reconciled in the WP-00 change.
