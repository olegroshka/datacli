---
id: OQ-002
title: What is a unit of fleet work, how is it claimed, and how is paid-once guaranteed?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - KB-001 G1, G2, RF2, RF3, scenarios S1, S3, S4
  - KB-002 section 2 (locks, fetch-state sidecars, snapshot outputs)
depends_on: [KB-001, KB-002, GLOSSARY, OQ-001]
referenced_by: [INV-001]
---

# OQ-002 - Work decomposition and claiming

## The question

Which pieces of today's work can be cut so that several nodes do them in
parallel, and what does owning a piece mean?

## Sub-questions

1. **Slice granularity per work type.**
   - Fetching: by lane (coarse, matches today's fetchers and sidecars), by
     ticker range within a lane (fine, needs sidecar partitioning), by day
     (natural for news).
   - Scoring and embeddings: by corpus partition (day range or symbol range);
     outputs must become partitioned files with a merge rule.
   - Local builds (reindex, daily tables): probably not worth splitting; one
     node does them after inputs exist. Are they fleet units at all?
2. **Two classes of unit.**
   - Paid units: a repeat costs money; need paid-once (G2).
   - Idempotent units: a repeat costs only time; optimistic claiming is fine.
   Should the protocol treat them differently (static assignment for paid,
   dynamic leases for idempotent)?
3. **Lease semantics.** Duration relative to expected runtime; renewal by
   heartbeat; what happens to partial output on expiry (resume from the unit's
   own checkpoint, or discard and redo); who may re-claim.
4. **Tie-breaks.** Two simultaneous claims: deterministic rule, and the
   loser's obligation (stop before any paid call).
5. **Dependencies.** Scoring a day needs that day's articles fetched: units
   carry inputs; a unit is claimable only when its inputs are visible in the
   store.
6. **Idempotency keys and checkpoints.** What identifies a unit across epochs,
   and how a node proves completion (pointer to output files with md5).

## Constraints from today's node

- Per-lane fetch-state sidecars are single files; splitting a lane across nodes
  means either partitioning the sidecar by owner or keeping lanes whole.
- Scheduler locks protect one machine; a fleet unit is a second, outer layer of
  exclusion, not a replacement.

## Criteria

- Paid-once holds even under the medium's weakest consistency (OQ-001).
- A unit's output is a disjoint set of remote files (feeds OQ-004).
- Every unit maps to exactly one registry command (G5).
- A human can read the unit list and see what was done by whom.

## Target resolution

A draft unit model (types, keys, lease rules) good enough to write DD-001 and
the two-node simulation, after OQ-001 is narrowed.
