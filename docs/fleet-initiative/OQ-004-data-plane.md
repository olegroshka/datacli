---
id: OQ-004
title: How do many nodes share one data plane with one writer per file?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.2
sources:
  - KB-001 G3, RF4, RF5, scenario S1, S7
  - KB-002 section 1 (storage) and 2 (push-only, one manifest)
  - docs/SCHEDULER_DAILY_RUN_RECOVERY.md (the duplicate-tree incident)
depends_on: [KB-001, KB-002, GLOSSARY, OQ-002]
referenced_by: [INV-001]
---

# OQ-004 - Data plane across nodes

## The question

Today "local is the truth" and sync is push-only from one root. With several
nodes producing and consuming, what is the truth, how do inputs reach the node
that needs them, and how is "one current copy per file" kept?

## Sub-questions

1. **Ownership.** Partition ownership follows slice ownership (RF4): the node
   that owns a slice is the only writer of its files. How is that recorded so a
   push from the wrong node is refused, not merely noticed?
2. **Per-node manifests versus a fleet manifest.** One manifest per node and
   data root (today) cannot describe files another node wrote. Options: a
   fleet manifest assembled from per-node manifests; or the store listing
   itself as the truth with md5 (what `reconcile` does today).
3. **Pull.** A scoring node needs news partitions fetched elsewhere. Sync must
   pull selected paths into a local root. Which paths, how much local storage,
   how staleness is shown.
4. **Partitioned outputs.** Scores, embeddings and the daily news tables are
   single snapshot files; splitting work means partitioned outputs with a
   documented merge (concatenate by key, last-writer-per-partition) and a
   consumer that can read partitions.
5. **Schema and version skew.** Outputs carry a contract version; a consumer
   refuses a partition it cannot read (S7).
6. **Store limits.** Drive API quota, listing cost, bandwidth per node; whether
   Drive stays the rendezvous for data while the ledger lives elsewhere
   (OQ-001), and what a successor store would need (object store with
   conditional writes would simplify ownership).

## Code facts that narrow the question (2026-09-12, SESSION-001-PREP section 2)

- The push planner has no write scope (include globs and excluded directory
  names only); orphans are reported, never deleted; trash is manual.
- The manifest is `<data_root>/.sync/<backend>.json`, entries `size,
  mtime_ns, md5, remote_id, uploaded_at`: the right shape for an output
  descriptor.
- No download, pull, ETag or conditional-write path exists in `storage/`; the
  backend port needs one `download` method.
- Drive folder creation is list-then-create and not atomic; `drive.file`
  visibility across devices that share one OAuth client is unverified (spike
  before ADR-003).
- A scoring node needs the day's article partition plus the lane price-state
  sidecars (the universe filter reads them).

## Owner decision (2026-09-13, D6)

The Linux box holds a full mirror of the data root, seeded once over the LAN
and kept current by pulling yesterday's descriptors from Drive each epoch.
Drive stays the plane of record with one current copy per file. The Linux
node's write scope is scores and embeddings only; the scoring `state.csv` is
node-local and never pushed.

Proposals P7 (completion records carry output descriptors), P8 (push write
scope enforced by the planner) and P14 (exactly three additions: `download`,
pull by descriptor, write scope) are the recommended shape for ADR-003.

## Constraints

- The Drive duplicate-tree incident is the cautionary tale: any design where
  two nodes can create the same path must be refused by construction.
- Push stays idempotent and md5-aware; reconcile stays the recovery path.

## Criteria

- One writer per remote file is enforced by the sync layer, not by convention.
- A node can rebuild its view of the fleet's data from the store alone.
- Pull and push use the same manifest model.
- Works with the local backend for tests.

## Target resolution

A partition and ownership model consistent with OQ-002's unit model, feeding
DD-001.
