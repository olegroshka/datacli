---
id: GLOSSARY
title: Fleet initiative terminology
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - docs/scheduler-initiative/GLOSSARY.md (terms reused, not redefined)
depends_on: []
referenced_by: [BOOTSTRAP-FLEET, KB-001, KB-002, INV-001]
---

# Fleet glossary (draft)

Scheduler terms (job, run, three planes, registry, journal, lock, desired /
backend / execution state) keep their meaning from the scheduler glossary and
are not redefined here.

| Term | Meaning |
|---|---|
| Fleet | The set of the owner's datacli nodes that share one data plane and one set of budgets. |
| Node | One datacli installation on one device, with a stable node id, its own scheduler, secrets, data root and agent. A fleet of one node is today's datacli. |
| Capability advertisement | A node's self-description used for work assignment: GPU class, has API key, has store token, bandwidth class, typical online window, code contract version. |
| Rendezvous store | The shared place nodes exchange data and coordination records; Google Drive today. Also called the data plane when talking about outputs. |
| Ledger | The append-only set of coordination records (claims, heartbeats, completions, proposals) that nodes read to learn fleet state. Medium undecided (OQ-001). |
| Unit | The smallest assignable piece of work: a registry command over one slice, with an idempotency key. |
| Slice | A named, disjoint part of a dataset: lane x dataset x ticker range, a day range, or a corpus partition. Slices are what units and partitions are keyed by. |
| Claim | A node's declaration that it owns a unit for the duration of a lease. |
| Lease | The time a claim is valid without renewal; expiry makes the unit re-claimable. |
| Idempotency key | The identity of a unit such that doing it twice is detectable and harmless (or forbidden, for paid units). |
| Partition ownership | The rule that the node owning a slice is the only writer of that slice's remote files. |
| Planner agent | The AI agent inside a node that proposes decompositions and negotiates. Proposes only. |
| Proposal | A ledger record describing a decomposition of pending work into units with suggested owners, subject to a protocol (OQ-003). |
| Epoch | A coordination round (for example one day) within which proposals, claims and budgets are scoped. |
| Budget envelope | The fleet-wide cap for a resource in an epoch: paid API calls per endpoint, model spend, store bandwidth. |
| Heartbeat | A periodic ledger record proving a node is online and still holding its claims. |
| Version skew | Nodes running different contract versions; tolerated within bounds, detected always. |
| Fleet view | The owner-facing report that lists every node's three planes and the ledger, without inferring one plane from another. |
