---
id: INV-001
title: Fleet initiative substrate inventory
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.2
sources:
  - docs/scheduler-initiative/INV-001 (structure reused)
depends_on: [KB-001, KB-002, GLOSSARY, OQ-001, OQ-002, OQ-003, OQ-004]
referenced_by: [BOOTSTRAP-FLEET]
---

# Fleet initiative inventory

## Register

| Id | File | Status | Version | Role |
|---|---|---|---|---|
| BOOTSTRAP-FLEET | `README.md` | DRAFT | 0.2 | entry point, ladder, protocol |
| KB-001 | `KB-001-fleet-charter.md` | DRAFT | 0.2 | intent, goals, requirements, invariants, scenarios |
| KB-002 | `KB-002-current-node.md` | DRAFT | 0.1 | what a node is today; single-node assumptions |
| GLOSSARY | `GLOSSARY.md` | DRAFT | 0.2 | terms |
| INV-001 | `INV-001-fleet-inventory.md` | DRAFT | 0.2 | this register |
| OQ-001 | `OQ-001-coordination-medium.md` | OPEN | 0.2 | where the ledger lives (leaning: git) |
| OQ-002 | `OQ-002-work-decomposition-and-claiming.md` | OPEN | 0.1 | units, slices, leases, paid-once |
| OQ-003 | `OQ-003-agent-agreement.md` | OPEN | 0.2 | negotiation layer: what agents decide and how they agree |
| OQ-004 | `OQ-004-data-plane.md` | OPEN | 0.1 | bidirectional sync, partitions, one writer |
| OQ-005 | `OQ-005-agentic-plane.md` | OPEN | 0.1 | provider tiers, harnesses, roles, caps |
| SESSION-001 | `SESSION-001-brainstorm-plan.md` | DRAFT | 0.2 | agenda for the first brainstorm |
| INV-002 | (not started) | - | - | work-type x capability matrix: which commands can be fleet units |
| INV-004 | (not started) | - | - | adversarial scenarios: split brain, stale leases, skew, quota races |
| DD-001 | (not started) | - | - | consensus layer: Node, Unit, Claim, Lease, Ledger port; provable over a fake medium |
| DD-002 | (not started) | - | - | git-ledger implementation design; held to the section 3a quality bar |
| DD-003 | (not started) | - | - | negotiation layer: proposal, critique, acceptance, inbox records, caps |
| ADR-001 | (not started) | - | - | ledger medium (leaning: git repository) |
| ADR-002 | (not started) | - | - | negotiation protocol (leaning: single planner per epoch with verifier review) |
| ADR-003 | (not started) | - | - | data-plane ownership and partitioning |
| ADR-004 | (not started) | - | - | provider policy and harness boundary |
| INV-003 | (not started) | - | - | dependency-ordered work packages |

## Priority queue for session 1

1. Sign or amend KB-001: goals, non-goals and the section 3a layering principle.
2. Confirm or reject the OQ-001 leaning (git ledger + Drive data plane) and
   define the spike that proves compare-and-set claiming.
3. Fill the OQ-003 decision surface; confirm the negotiation protocol for the spike.
4. Settle the OQ-005 provider policy: tiers, caps, what runs headless where.
5. Rank scenarios S1 to S9.

## Dependency map

```text
GLOSSARY --> KB-001 (3a layering) --> OQ-001 (medium)  --> ADR-001 --> DD-002 (git ledger, quality bar)
                |                 --> OQ-002 (units)   --> DD-001 (consensus) --> INV-002 --> INV-003
                |                 --> OQ-003 (agents)  --> ADR-002 --> DD-003 (negotiation)
                |                 --> OQ-005 (agentic) --> ADR-004
KB-002 ---------+-----------------> OQ-004 (data)     --> ADR-003
INV-004 (adversarial) informs every ADR before acceptance
```

## Health

All artefacts are drafts by design. No artefact may move to STABLE before the
owner has amended and accepted KB-001.
