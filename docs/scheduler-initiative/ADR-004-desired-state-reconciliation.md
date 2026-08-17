---
id: ADR-004
title: Separate desired, backend and execution state with immutable definition snapshots
status: STABLE
decision_state: ACCEPTED
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.0
sources:
  - KB-001
  - KB-002
  - INV-004
depends_on: [KB-001, KB-002, ADR-001, GLOSSARY]
referenced_by: [INV-001, INV-003, INV-004, DD-001, OQ-001]
---

# ADR-004 - Separate desired, backend and execution state with immutable definition snapshots

## Approval warning

Accepted by the owner through the scheduler implementation authorization on
2026-08-17, including OQ-001's recommended profile-local store.

## Context

A schedule update spans at least a file store and Windows Task Scheduler. There
is no atomic transaction across them. A task may also launch with an old action
while an edit or delete is in flight. Keeping only the current `JobSpec` plus a
digest makes historical runs irreproducible after that definition changes.

The original substrate named drift, but did not define an authoritative side,
safe operation ordering or race behaviour. INV-004 shows that this omission can
execute stale work or falsely report a successful update.

## Proposed decision

Use three explicit state planes:

1. **Desired state** - a profile-local file store containing a current job
   pointer (or tombstone), monotonically increasing generation and immutable,
   content-addressed `JobSpec` snapshots.
2. **Backend state** - the installed Windows task and observed scheduler
   metadata, including the expected definition digest/generation embedded in
   its one runner action.
3. **Execution state** - per-run append-only journal events and logs, each
   pointing to the immutable snapshot actually loaded.

The desired-state pointer is authoritative. Backend installation is a
reconciliation operation, not the commit record for job meaning. Status shows
all three planes and a derived reconciliation state such as `in_sync`,
`missing_task`, `stale_task`, `orphan_task`, `install_failed`,
`delete_pending`, `incompatible` or `unknown`.

### Update protocol

1. Serialize management operations under a per-job management lock.
2. Validate the new definition and write its immutable snapshot atomically.
3. Atomically compare-and-swap the desired pointer to the next generation.
4. Install/replace the OS task with an action containing profile ID, job ID,
   generation and definition digest.
5. Query and compare backend state. On failure, retain the new desired state
   and report reconciliation failure; do not pretend the two writes rolled
   back atomically.

During the reconciliation window an old action fails closed at runner startup
because its digest/generation no longer matches the current desired pointer.

### Runner snapshot protocol

The runner receives an expected generation and digest. It first creates a
minimal durable run-start event naming that expectation, then loads the
immutable snapshot and checks the active desired pointer/tombstone. It performs
environment preflight, acquires job/resource locks and rechecks the active
pointer before the first mutation. A missing snapshot becomes `invalid`; a
pointer mismatch becomes `stale_dispatch`. It never substitutes the newest
definition silently. Once mutation begins, the loaded snapshot remains fixed
for that run; a later edit affects future runs only.

### Delete protocol

Delete refuses by default while a run is active. Deletion writes a tombstone,
then disables/removes the OS task and verifies absence. Failure leaves
`delete_pending`/`orphan_task` for explicit reconciliation. Delete does not
cancel a run, purge history or remove snapshots referenced by history. Stop and
purge are separate explicit operations.

### Repair policy

`status` and `doctor` are read-only. Repair occurs only through an explicit
management operation such as `schedule reconcile`; it uses generation checks
and never adopts an unknown task as desired state automatically.

## Alternatives considered

### A. Treat Task Scheduler XML as authoritative

Rejected because it duplicates datacli workflow meaning, cannot preserve the
full portable run contract, and makes backend replacement a migration of the
domain model.

### B. Roll back the file if task installation fails

Rejected as a guarantee. A crash can occur before rollback, and the previous
task may already have been replaced. Explicit degraded reconciliation is more
truthful and recoverable.

### C. Always execute the newest definition for a job ID

Rejected because an already-queued old action could unexpectedly execute newly
edited work, while historical attribution would be ambiguous.

### D. Execute the digest named by any installed action forever

Rejected because a stale or orphaned OS task could continue running after the
user edits or deletes the job.

## Consequences

### Positive

- Update/delete races fail closed.
- Historical runs resolve to exact non-secret definitions.
- Partial management failures remain diagnosable and repairable.
- Backend replacement does not redefine job meaning.

### Costs and risks

- The store needs generations, snapshots, tombstones and reference-aware
  retention rather than one mutable JSON file per job.
- A short failed-closed window can skip a dispatch during reconciliation.
- Status and repair logic are more substantial than a direct `schtasks` wrapper.
- Store and log ACLs, disk-full handling and migration need explicit tests.

## Acceptance criteria

- Fault injection at every update/delete phase never executes a different
  definition from the one named in the run record.
- A stale action produces `stale_dispatch` without domain mutation.
- Status distinguishes desired, backend and execution observations.
- Any retained run can load its exact definition snapshot after edit/delete.
- Reconciliation and purge are explicit, idempotent operations.

## Decision outcome

Accepted on 2026-08-17. OQ-001 is resolved to the recommended per-user profile
store with generated UUID identity.
