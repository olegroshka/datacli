---
id: INV-001
title: Fleet initiative substrate inventory
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.4
sources:
  - docs/scheduler-initiative/INV-001 (structure reused)
depends_on: [KB-001, KB-002, GLOSSARY, OQ-001, OQ-002, OQ-003, OQ-004, OQ-005, OQ-006, SESSION-001-PREP]
referenced_by: [BOOTSTRAP-FLEET]
---

# Fleet initiative inventory

## Register

| Id | File | Status | Version | Role |
|---|---|---|---|---|
| BOOTSTRAP-FLEET | `README.md` | DRAFT | 0.3 | entry point, ladder, protocol |
| KB-001 | `KB-001-fleet-charter.md` | DRAFT | 0.2 | intent, goals, requirements, invariants, scenarios |
| KB-002 | `KB-002-current-node.md` | DRAFT | 0.2 | what a node is today; single-node assumptions |
| GLOSSARY | `GLOSSARY.md` | DRAFT | 0.3 | terms |
| INV-001 | `INV-001-fleet-inventory.md` | DRAFT | 0.4 | this register |
| OQ-001 | `OQ-001-coordination-medium.md` | OPEN | 0.3 | where the ledger lives (decided in principle: private git repository, D11; ADR-001 after the S4U spike) |
| OQ-002 | `OQ-002-work-decomposition-and-claiming.md` | OPEN | 0.2 | units, slices, leases, paid-once (scoring unit fixed by code; D2, D5, D8, D9 recorded) |
| OQ-003 | `OQ-003-agent-agreement.md` | OPEN | 0.3 | negotiation layer (D7 two-tier approval, D13 agent duties recorded) |
| OQ-004 | `OQ-004-data-plane.md` | OPEN | 0.2 | bidirectional sync, partitions, one writer (D6 mirror recorded) |
| OQ-005 | `OQ-005-agentic-plane.md` | OPEN | 0.2 | provider tiers, harnesses, roles, caps (D3 batched local tier recorded) |
| OQ-006 | `OQ-006-linux-node-scheduling.md` | OPEN | 0.1 | Linux node fleet tick: cron first (D10), systemd adapter later |
| SESSION-001 | `SESSION-001-brainstorm-plan.md` | DRAFT | 0.3 | agenda for the first brainstorm; outcomes so far |
| SESSION-001-PREP | `SESSION-001-prep.md` | DRAFT | 0.2 | substrate critique, code findings F1-F22, proposals P1-P15, revised trajectory, INV-004 seeds, agreed design and decisions D1-D13 |
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

## Priority queue (after the 2026-09-13 design rounds)

1. Sign or amend KB-001: goals (SESSION-001-PREP section 4 amendments),
   non-goals and section 3a. Accept, amend or reject proposals P1 to P15.
2. Phase 1a: scoring runner concurrency and endpoint-defined "local" (D3, D4).
3. Phase 1b: the S4U git fetch spike on the Windows box (D11).
4. Phase 1c: DD-001 with the Ledger and Planner ports; INV-004 seeds; ADR-001
   and ADR-004 PROPOSED.
5. Rank scenarios S1 to S9; answer the open owner questions 2, 7 and 9.

## Dependency map

```text
GLOSSARY --> KB-001 (3a layering) --> OQ-001 (medium)  --> ADR-001 --> DD-002 (git ledger, quality bar)
                |                 --> OQ-002 (units)   --> DD-001 (consensus) --> INV-002 --> INV-003
                |                 --> OQ-003 (agents)  --> ADR-002 --> DD-003 (negotiation)
                |                 --> OQ-005 (agentic) --> ADR-004
                |                 --> OQ-006 (linux tick) --> (adapter ADR later)
KB-002 ---------+-----------------> OQ-004 (data)     --> ADR-003
INV-004 (adversarial) informs every ADR before acceptance
SESSION-001-PREP records code findings, proposals and decisions D1-D13 feeding every OQ
```

## Health

All artefacts are drafts by design. No artefact may move to STABLE before the
owner has amended and accepted KB-001.
