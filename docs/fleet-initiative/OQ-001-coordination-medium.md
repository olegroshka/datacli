---
id: OQ-001
title: Where does the fleet ledger live, with no always-on server?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.3
sources:
  - KB-001 G4, G7, G10, RN1, RN2, section 3a
  - KB-002 section 2
  - Owner discussion 2026-09-12 (no extra cost; home boxes only when needed)
depends_on: [KB-001, KB-002, GLOSSARY]
referenced_by: [INV-001]
---

# OQ-001 - Coordination medium (the consensus layer's carrier)

## The question

Nodes must learn who is online, what is claimed, what is done and how much
budget is left, while any node may be asleep for days and there is no server.
Where do those records live, and what consistency can we count on?

This question decides only the **consensus layer's medium** (KB-001 3a). The
negotiation layer may reuse the same medium for its records, but that reuse is
a separate, later choice; nothing here is allowed to shape the negotiation
protocol.

## Why it matters

Every other question inherits this one. Leases, paid-once (G2) and budgets
(G7) are only as strong as the medium's ordering and visibility guarantees.
The owner's constraints narrow the field: no extra cost, no server to babysit,
personal devices that sleep.

## Options

| Option | Sketch | Pull | Push |
|---|---|---|---|
| A. Store-as-ledger | Small records as files in the rendezvous store (Drive), append-only, named by (epoch, unit, node). | Nothing new to run; same auth; records travel with the data. | Eventual consistency: two claims can coexist briefly; listing cost grows; Drive API quota. Paid-once rests on tie-breaks and back-off, not on atomicity. |
| B. Elected coordinator | Oldest online node acts as coordinator for an epoch. | Simple inside an epoch. | Election over an eventually consistent medium is the hard problem; coordinator sleeps mid-epoch. |
| C. Tiny hosted service | Minimal always-on endpoint holding the ledger. | Atomic claims; trivial budgets. | A server, with uptime, secrets and cost. Conflicts with the owner's "no extra cost, no babysitting". |
| D. Git repository as ledger | Records are commits; branches or refs per node; a push to a ref is a compare-and-set. | Free on a private hosted repo; atomic append; full history; human-readable text; agents are natively fluent in git; conflicts are a contention signal, not a failure. | Not built for heartbeats (needs a cadence and compaction); a hosted git is an external service like Drive; concurrent pushes need a retry discipline; repository growth needs retention rules. |

## Leaning (2026-09-12, not a decision)

**D for the consensus ledger, Drive stays the data plane.** Reasons: it is the
only free option that gives real atomic claiming (first push to the ref wins,
the loser rebases and sees the claim before any paid call), it needs no
operating, and it is the medium agents work best in. Static assignment of paid
fetch work per key-holding device (OQ-002) reduces how much the consensus layer
has to arbitrate at all.

Condition attached by the owner: the git-ledger implementation design must be
ultra-high quality, tight and clean. It gets its own DD-002 with minimal
semantics (what a ref means, what a record is, what a push proves), a small
versioned record schema, retention and compaction rules, and exhaustive
adversarial tests over a fake git and a real remote, all reviewed before any
product code.

## Owner decision in principle (2026-09-13, D11; see SESSION-001-PREP 10.5)

Option D confirmed: a private GitHub repository under the owner's account,
one deploy key per device, a policy ref that nodes read and only the owner
writes. The decision becomes ADR-001 (PROPOSED) after the first spike: a
harmless logged-off (S4U) task on the Windows box fetching the empty
repository. If that fetch fails, the fallback is a small interactive-session
task that writes the daily job's two records (lane claims, news completion);
the ledger choice stands either way. Proposals P3 (no heartbeat record; lease
renewal by compare-and-set) and P4 (a two-operation Ledger port: `read`,
`cas`) shape DD-001 and DD-002 and are not yet accepted.

## Criteria

1. Works with every node asleep except one (G4).
2. Paid-once can be enforced, not merely hoped (G2).
3. Coordination traffic stays small relative to data traffic (RN2).
4. No new secrets and no secret transport (G9); a hosted repo needs its own
   per-device credential, which must be as contained as the Drive token.
5. Testable with a fake medium in-process (RN4).
6. Zero recurring cost; no operating burden (owner constraint).

## What we need to learn (the spike)

- Compare-and-set claiming over a real hosted repo from two nodes within
  seconds: does the loser reliably observe the winner before acting?
- Heartbeat cadence versus repository growth; a compaction rule that keeps
  history auditable but the working set small.
- Whether the same repository can safely carry negotiation records or whether
  they belong in a second, lower-stakes ref or repository.
- Credential handling for a logged-off (S4U) node.

## Target resolution

Leaning confirmed or replaced in session 1; spike results feed ADR-001 and
DD-002.
