---
id: INV-001
title: Scheduler initiative substrate inventory
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - Shared Substrate v0.2, sections 4 and Appendix A
depends_on: [KB-001, KB-002, INV-002, INV-003, INV-004, DD-001, ADR-001, ADR-002, ADR-003, ADR-004, ADR-005, OQ-001, OQ-002, OQ-003, GLOSSARY]
referenced_by: [BOOTSTRAP-SCHEDULER]
---

# Scheduler initiative substrate inventory

## Health summary

- Initiative phase: implementation and verification.
- Stable artefacts: 17, including this inventory and the bootstrap pointer.
- Accepted decisions: ADR-001, ADR-002, ADR-003, ADR-004, ADR-005.
- Resolved questions: OQ-001, OQ-002, OQ-003; each uses its documented
  recommended option.
- Implementation authorised: yes, by the owner on 2026-08-17.

## Artefact register

| ID | File | Status | Version | Owns |
|---|---|---:|---:|---|
| BOOTSTRAP-SCHEDULER | `README.md` | STABLE | 1.2 | warm-up pointer and light protocol |
| INV-001 | `INV-001-substrate-inventory.md` | STABLE | 1.2 | artefact register, health, priority queue |
| KB-001 | `KB-001-initiative-charter.md` | STABLE | 1.0 | intent, scope, scenarios, success, invariants |
| KB-002 | `KB-002-current-system.md` | STABLE | 1.2 | verified facts about current datacli execution |
| INV-002 | `INV-002-command-capability.md` | STABLE | 1.1 | schedulable command capability matrix |
| INV-003 | `INV-003-implementation-work-packages.md` | STABLE | 1.2 | dependency-ordered technical expansion |
| INV-004 | `INV-004-adversarial-scenarios.md` | STABLE | 1.2 | adversarial scenarios, corrected guarantees and failure-mode oracles |
| DD-001 | `DD-001-scheduler-domain-contracts.md` | STABLE | 1.2 | domain records, ports, states, execution semantics |
| ADR-001 | `ADR-001-shared-execution-substrate.md` | STABLE / ACCEPTED | 1.0 | shared execution substrate and adapter boundary |
| ADR-002 | `ADR-002-windows-task-scheduler-adapter.md` | STABLE / ACCEPTED | 1.2 | native Windows scheduling adapter |
| ADR-003 | `ADR-003-allowlisted-argv-workflows.md` | STABLE / ACCEPTED | 1.0 | allowed command representation and workflow semantics |
| ADR-004 | `ADR-004-desired-state-reconciliation.md` | STABLE / ACCEPTED | 1.0 | desired/backend/execution state and immutable snapshots |
| ADR-005 | `ADR-005-locale-neutral-windows-observation.md` | STABLE / ACCEPTED | 1.1 | fixed read-only locale-neutral Task Scheduler observation |
| OQ-001 | `OQ-001-definition-storage-and-profile-identity.md` | STABLE / RESOLVED | 1.0 | definition location and profile identity |
| OQ-002 | `OQ-002-windows-runtime-policy.md` | STABLE / RESOLVED | 1.0 | Windows principal, dispatch, time and power defaults |
| OQ-003 | `OQ-003-mvp-command-and-workflow-ux.md` | STABLE / RESOLVED | 1.0 | first-release command set and workflow UX |
| GLOSSARY | `GLOSSARY.md` | STABLE | 1.0 | authoritative scheduler terminology |

## Priority queue

| Priority | Action | Completion oracle |
|---:|---|---|
| P1 | Complete disruptive Windows environment matrices only with explicit authorisation | logout/lock, sleep, battery, timezone/DST and non-English-host evidence |
| P0 | Preserve honest cancellation/dispatch claims | corresponding INV-004 oracle has evidence before strong wording is used |
| P1 | Reverse-propagate implementation discoveries | owning artefact and `referenced_by` closure stay synchronized |

## Propagation map

| If this changes | Review at minimum |
|---|---|
| KB-001 intent or scope | INV-002, INV-003, INV-004, DD-001, ADR-001, ADR-003, ADR-004, OQ-002, OQ-003 |
| KB-002 current-system fact | INV-002, INV-003, INV-004, DD-001, ADR-001..004, OQ-001, OQ-002 |
| INV-002 command support | DD-001, ADR-003, OQ-003, INV-003 |
| INV-004 adversarial disposition | DD-001, ADR-002, ADR-004, OQ-002, OQ-003, INV-003 |
| DD-001 contract | INV-003 and every adapter/runner implementation derived from it |
| Accepted ADR | DD-001, relevant OQs, INV-003 |
| Implementation discovery contradicting an upper layer | stop; draft a new ADR; mark affected upstream/downstream artefacts STALE |

ADR-005 records the WP-04 discovery that `schtasks /Query /XML` cannot provide
the required locale-neutral runtime observation fields. Its accepted narrow
read-only probe keeps the affected closure stable.

## Status transitions for this initiative

- `DRAFT -> STABLE`: human owner review, internal cross-references agree, and
  no unresolved choice is presented as settled.
- `STABLE -> STALE`: a dependency changes substantively, current datacli
  behaviour changes, or implementation reveals a false assumption.
- `STALE -> STABLE`: impact review and re-approval.
- Any -> `DEPRECATED`: a successor artefact is named; history remains.
