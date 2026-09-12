---
id: BOOTSTRAP-FLEET
title: Fleet initiative substrate entry point (datacli on many nodes)
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.2
sources:
  - Shared Substrate v0.2 conventions, as practised in docs/scheduler-initiative
depends_on: [KB-001, GLOSSARY]
referenced_by: []
---

# Fleet initiative: from one datacli node to many

This directory is **scaffolding**. It frames a problem and organises the
thinking; it decides nothing. No artefact here is STABLE, no ADR exists, and no
code should be written against it until KB-001 is accepted and the open
questions have recommended options.

## The problem in one paragraph

Today datacli is one node: one repo checkout, one data root, one scheduler, one
paid API key, one Google Drive target. The owner has several devices, some with
strong GPUs, that sit idle while the one node fetches, builds and (rarely)
scores. The intent is a **fleet** of datacli nodes that share one data plane
(Drive today) and split the work among themselves: fetching different slices of
the market data, computing scores and embeddings on whichever GPU is free,
pushing results without stepping on each other, and doing all of this without
an always-on server, because the devices are personal machines that sleep,
reboot and travel. Inside each node an AI agent should be able to take part in
deciding how the work is cut, while execution stays on the deterministic,
allowlisted substrate the scheduler initiative built.

## Structural principle

Consensus (who owns what), negotiation (how agents agree on a plan) and
policy (the owner's caps and gates) are separate abstract layers with
separate contracts and separate decision records; see KB-001 section 3a.
The ledger implementation that carries consensus, leaning git, is held to an
ultra-high quality bar. Leanings from the 2026-09-12 discussion live in each
OQ under "Leaning" and are not decisions.

## What this initiative builds on

The scheduler initiative (`docs/scheduler-initiative/`) gives every node a
command registry, a runner with journal and locks, a Windows adapter and three
honest state planes. The storage layer gives an idempotent push with a
per-root manifest and a reconcile path. The fleet adds a **coordination layer
above nodes**, it does not replace anything below. Where a fleet need
contradicts a node assumption (machine-wide locks, push-only sync, one fetch
state per lane), that contradiction is recorded in KB-002 and becomes an open
question here, not a silent redesign there.

## Abstraction ladder and current state

| Layer | Artefact | Status |
|---|---|---|
| Intent, goals, requirements, scenarios | KB-001 | DRAFT (for brainstorm session 1) |
| Facts about today's single node | KB-002 | DRAFT (code-grounded) |
| Terms | GLOSSARY | DRAFT |
| Register, priorities, dependencies | INV-001 | DRAFT |
| Coordination medium | OQ-001 | OPEN |
| Work decomposition and claiming | OQ-002 | OPEN |
| Agent agreement protocol | OQ-003 | OPEN |
| Data plane across nodes | OQ-004 | OPEN |
| Agentic plane: providers, harnesses, roles, caps | OQ-005 | OPEN |
| Capability / work-type matrix | INV-002 | NOT STARTED |
| Adversarial scenarios | INV-004 | NOT STARTED |
| Consensus-layer contracts | DD-001 | NOT STARTED |
| Git-ledger implementation design (quality bar, KB-001 3a) | DD-002 | NOT STARTED |
| Negotiation-layer contracts | DD-003 | NOT STARTED |
| Decision records | ADR-001.. | NOT STARTED |
| Work packages | INV-003 | NOT STARTED |

## Warm-up for any session on this topic

1. Read KB-001 for intent and the draft goals; argue with it.
2. Read KB-002 for what is true today and what is inherently single-node.
3. Use GLOSSARY terms exactly; propose new ones there, not inline.
4. Pick the OQ being discussed; leave the others closed.
5. Park implementation ideas in `SESSION-001-brainstorm-plan.md` under
   "parking lot"; nothing in this directory is an implementation plan yet.

## Edit protocol

Same light protocol as the scheduler substrate: identify the single source of
truth for the fact being changed, edit the minimum surface, cite by stable id,
bump `version` and `last_reviewed`, update INV-001 in the same change. A
decision produces an ADR; an unresolved choice produces or extends an OQ.
