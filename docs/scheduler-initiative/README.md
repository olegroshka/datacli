---
id: BOOTSTRAP-SCHEDULER
title: Scheduler initiative substrate entry point
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - Shared Substrate v0.2 (2026-08-01)
depends_on: [INV-001, GLOSSARY]
referenced_by: []
---

# Scheduler initiative

This directory is the shared substrate for adding managed, recurring datacli
jobs. It fixes intent, vocabulary, contracts, and decision boundaries before
implementation. It is deliberately mid-light: enough structure to prevent
drift and phantom decisions, without graph tooling or process ceremony that
would outweigh this initiative.

Implementation was authorised by the owner on 2026-08-17 with the documented
recommended options in OQ-001 through OQ-003. ADR-001 through ADR-005 are
accepted and the substrate is the binding implementation contract.

The authorised harmless Windows lifecycle completed on 2026-08-17: one
manual, read-only job was registered, observed, dispatched to a durable
successful run, tombstoned and proven absent. No network, credential or paid
operation was used.

## Warm-up

For any scheduler work:

1. Read [INV-001](INV-001-substrate-inventory.md).
2. Read [KB-001](KB-001-initiative-charter.md) for intent and scope.
3. Follow the `depends_on` closure for the task being considered.
4. Treat [GLOSSARY](GLOSSARY.md) as the semantic authority.
5. Do not treat a proposed ADR as an accepted decision.

This file is a pointer to canonical artefacts, not a restatement of them.

## Abstraction ladder and oracles

| Layer | Canonical artefact | Representation | Oracle |
|---|---|---|---|
| Intent and success | KB-001 | structured natural language and scenarios | human review |
| Existing constraints | KB-002 | code-grounded facts | source inspection and current CLI execution |
| Supported surface | INV-002 | exhaustive capability matrix | registry parity and dry-run/parse tests |
| Adversarial boundary | INV-004 | failure-mode/scenario matrix | fault injection and Windows integration evidence |
| Domain and ports | DD-001 | typed records, state transitions, port contracts | schema, type, unit, and contract tests |
| Load-bearing choices | ADR-001..005 | append-only decision records | explicit human approval |
| Unresolved policy | OQ-001..003 | options, criteria, target date | human resolution into ADRs |
| Expansion into code | INV-003 | dependency-ordered work packages | package-level acceptance checks |

Implementation is a forward propagation from stable upper layers. If
implementation discovers a missing or false upper-layer assumption, reverse
propagation must be recorded in a new ADR; the code must not silently redefine
the substrate.

## Single-source-of-truth map

- Intent, scope, success and invariants: KB-001.
- Facts about current datacli behaviour: KB-002.
- Which existing commands may be scheduled: INV-002.
- Known edge cases, contradicted guarantees and required behaviour: INV-004.
- Job, trigger, workflow, run-state and port contracts: DD-001.
- Architectural choices: ADR-001..005 after approval.
- Deliberately unresolved choices: OQ-001..003.
- Technical expansion order: INV-003.
- Terms: GLOSSARY.

Do not duplicate those facts in this bootstrap file.

## Light edit protocol

For a substantive change:

1. Read INV-001 and the target artefact frontmatter.
2. Identify the SSOT for the fact being changed.
3. Walk `referenced_by` and identify downstream propagation.
4. Edit the minimum surface; cite other artefacts by stable ID.
5. Update metadata and INV-001 in the same change.
6. Draft an ADR for a choice or an OQ for an unresolved question.

Typos, formatting and non-semantic link repairs use the trivial lane: update
`last_reviewed`, but no impact sweep or decision record is required.

## Human review order

1. KB-001: is this the intended product and boundary?
2. ADR-001: is the shared execution substrate the right architecture?
3. ADR-003: is an allowlisted command model the right safety boundary?
4. ADR-002: is native Windows Task Scheduler the right first adapter?
5. INV-004: are the corrected guarantees and adversarial behaviours honest?
6. ADR-004: is the desired/backend/execution reconciliation model acceptable?
7. DD-001: are the domain contracts complete enough to implement from?
8. OQ-001..003: resolve the remaining machine/user policy choices.
9. ADR-005: confirm the implementation-discovered observation refinement.
10. INV-003: confirm that implementation is now a technical expansion.
