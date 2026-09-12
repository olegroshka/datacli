---
id: OQ-004
title: How do many nodes share one data plane with one writer per file?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
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
