---
id: GLOSSARY
title: Fleet initiative terminology
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.3
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
| Heartbeat | A periodic ledger record proving a node is online and still holding its claims. Proposal P3 (SESSION-001-PREP) replaces it with lease renewal: liveness is derived from lease freshness and no separate record exists. Not yet decided. |
| Version skew | Nodes running different contract versions; tolerated within bounds, detected always. |
| Fleet view | The owner-facing report that lists every node's three planes and the ledger, without inferring one plane from another. |
| Consensus layer | The deterministic, model-free layer that decides ownership: claims, leases, tie-breaks, completions, budget counters, liveness. Implemented over the ledger medium. |
| Negotiation layer | The model-driven layer in which agents propose, critique and accept plans through addressed ledger records. Produces inputs to consensus, never ownership. |
| Policy layer | The owner's caps, gates, defaults, vetoes and provider tiers. Above both other layers. |
| Provider tier | A class of model access chosen per task: local open-weight on a node's GPU; subscription harness (Claude, OpenAI) on a signed-in device; pay-per-token API under an explicit budget only. |
| Harness | The agent runtime a node uses (for example Claude Code or Codex headless, or a local runner) that reaches datacli through its MCP tools and the registry. |
| Role | An epoch assignment an online node takes: planner (writes the epoch plan), worker (executes claimed units, no model needed), verifier (reconciles and reviews), scout (watches for drift and failure). |
| Epoch plan | The planner's record of pending units, proposed owners and budgets for one epoch; accepted through the negotiation protocol; realised through consensus claims. |
| Inbox record | An addressed ledger record from one node's agent to another (question, critique, request); the unit of asynchronous agent interaction. |
| Pinned unit | A unit whose owner is fixed by policy for an epoch (paid fetch lanes in v1). The pinned node still writes its claim through the consensus path; it is uncontended by construction. |
| Claimable unit | A unit any capable node may claim with a lease (scoring and embedding partitions). A repeat costs GPU time, never money. |
| Pass | An owner-approved scoring campaign: one `(schema@v, model, quantisation)`, a day window, an envelope of GPU hours. The planner emits units only for approved passes. Mixed models across days are forbidden inside a pass. |
| Fleet tick | The periodic registry command a node runs to take part in the fleet: read the ledger, plan if no plan exists, claim, pull, work, push, complete, renew. |
| Output descriptor | The entry a completion record carries per written file: relpath, size, md5, remote id, contract fields. Same shape as a manifest entry; consumers pull by descriptor and verify md5. |
| Write scope | The set of path prefixes a node may push in an epoch: its claimed units' partitions plus node-scoped paths. Enforced by the push planner. Single-node mode has scope "everything". |
| Mirror | A node's full local replica of the data root, seeded once and kept current by pulling descriptors; the node's write scope stays narrow. |
| Local endpoint | A model-serving endpoint on the node itself (batched OpenAI-compatible server or Ollama), declared in policy with the model ids it serves; calls to it cost nothing. |
| Quality gate | The verifier's periodic check of a pass against the existing eval and panel tooling; a failed gate pauses the pass and notifies the owner. |
