---
id: INV-001
title: Fleet initiative substrate inventory
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - docs/scheduler-initiative/INV-001 (structure reused)
depends_on: [KB-001, KB-002, GLOSSARY, OQ-001, OQ-002, OQ-003, OQ-004]
referenced_by: [BOOTSTRAP-FLEET]
---

# Fleet initiative inventory

## Register

| Id | File | Status | Version | Role |
|---|---|---|---|---|
| BOOTSTRAP-FLEET | `README.md` | DRAFT | 0.1 | entry point, ladder, protocol |
| KB-001 | `KB-001-fleet-charter.md` | DRAFT | 0.1 | intent, goals, requirements, invariants, scenarios |
| KB-002 | `KB-002-current-node.md` | DRAFT | 0.1 | what a node is today; single-node assumptions |
| GLOSSARY | `GLOSSARY.md` | DRAFT | 0.1 | terms |
| INV-001 | `INV-001-fleet-inventory.md` | DRAFT | 0.1 | this register |
| OQ-001 | `OQ-001-coordination-medium.md` | OPEN | 0.1 | where the ledger lives |
| OQ-002 | `OQ-002-work-decomposition-and-claiming.md` | OPEN | 0.1 | units, slices, leases, paid-once |
| OQ-003 | `OQ-003-agent-agreement.md` | OPEN | 0.1 | what agents decide and how they agree |
| OQ-004 | `OQ-004-data-plane.md` | OPEN | 0.1 | bidirectional sync, partitions, one writer |
| SESSION-001 | `SESSION-001-brainstorm-plan.md` | DRAFT | 0.1 | agenda for the first brainstorm |
| INV-002 | (not started) | - | - | work-type x capability matrix: which commands can be fleet units |
| INV-004 | (not started) | - | - | adversarial scenarios: split brain, stale leases, skew, quota races |
| DD-001 | (not started) | - | - | domain records and ports: Node, Unit, Claim, Ledger, Proposal |
| ADR-001.. | (not started) | - | - | decisions from the OQs |
| INV-003 | (not started) | - | - | dependency-ordered work packages |

## Priority queue for session 1

1. Agree or amend the goals and non-goals in KB-001 (section 3 and 4).
2. Narrow OQ-001 to two options with the evidence needed to choose.
3. Narrow OQ-003: the agent's decision surface and the owner's gates.
4. Record the scenarios the owner cares about most (KB-001 section 7).

## Dependency map

```text
GLOSSARY --> KB-001 --> OQ-001 (medium) --> ADR-001
                |   --> OQ-002 (units)  --> DD-001 --> INV-002 --> INV-003
                |   --> OQ-003 (agents) --> ADR-002
KB-002 ---------+-- --> OQ-004 (data)   --> ADR-003
INV-004 (adversarial) informs every ADR before acceptance
```

## Health

All artefacts are drafts by design. No artefact may move to STABLE before the
owner has amended and accepted KB-001.
