---
id: OQ-001
title: Where does the fleet ledger live, with no always-on server?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - KB-001 G4, G7, RN1, RN2
  - KB-002 section 2
depends_on: [KB-001, KB-002, GLOSSARY]
referenced_by: [INV-001]
---

# OQ-001 - Coordination medium

## The question

Nodes must learn who is online, what is claimed, what is done and how much
budget is left, while any node may be asleep for days and there is no server.
Where do those records live, and what consistency can we count on?

## Why it matters

Every other question inherits this one. Leases, paid-once (G2) and budgets
(G7) are only as strong as the medium's ordering and visibility guarantees.
Drive is eventually consistent and has API quotas; a personal device is not
always on; a tiny cloud service is a server the owner said they did not want
to babysit.

## Options (to narrow, not to decide)

| Option | Sketch | Pull | Push |
|---|---|---|---|
| A. Store-as-ledger | Coordination records are small files in the rendezvous store (Drive): one file per claim/heartbeat/completion, append-only, named by (epoch, unit, node). Nodes list and read them. | Nothing new to run; same auth; records travel with the data. | Eventual consistency means two claims can coexist briefly; listing cost grows with records; Drive API quota. |
| B. Elected coordinator | Whichever node is online and oldest acts as coordinator for an epoch; others talk to it through the store or LAN. | Simpler reasoning inside an epoch. | Election over an eventually consistent medium is itself the hard problem; coordinator sleep mid-epoch. |
| C. Tiny hosted service | A minimal always-on endpoint (cheapest cloud tier, or a Raspberry-class device at home) holding the ledger with strong ordering. | Real atomic claims; trivial budgets. | It is a server: uptime, secrets, cost, another thing to fix when it breaks. |
| D. Git repository as ledger | Records are commits to a small private repo; pushes are atomic per ref. | Atomic append with history; tooling exists. | Conflicts on concurrent pushes need retry; not designed for heartbeats; a hosted git is also a service. |

## Criteria

1. Works with every node asleep except one (G4).
2. Paid-once can be enforced, not merely hoped, or the residual risk is
   bounded and priced (G2).
3. Coordination traffic stays small relative to data traffic (RN2).
4. No new secrets and no secret transport (G9).
5. Testable with a fake medium in-process (RN4).
6. Operational burden the owner accepts.

## What we need to learn

- Drive's actual consistency and listing latency for small files written by
  two clients within seconds; whether a deterministic tie-break (lowest node id
  wins, loser backs off) makes option A good enough for paid-once.
- Whether a conservative static partitioning of paid work (by lane, fixed per
  epoch) removes the need for atomic claims for fetching altogether, leaving
  dynamic claiming only for idempotent GPU work where a repeat is merely waste.
- The owner's tolerance for option C.

## Target resolution

Two options carried into ADR-001 with a spike plan, by the end of session 1.
