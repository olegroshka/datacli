---
id: OQ-003
title: What do the planner agents decide, and how do they agree?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.1
sources:
  - KB-001 G5, RF6, RF7, scenario S5
  - docs/scheduler-initiative/ADR-003 (allowlisted execution)
depends_on: [KB-001, GLOSSARY, OQ-002]
referenced_by: [INV-001]
---

# OQ-003 - Agent agreement

## The question

The owner wants agents inside the nodes to agree on how to slice the problem
and work on it together. Which decisions are theirs, which are policy, and what
protocol turns several opinions into one plan without a server?

## The decision surface (to be drawn)

| Candidate decision | Agent, policy, or owner? |
|---|---|
| Which units exist today (pending work discovered from store and sidecars) | policy can compute this deterministically; does an agent add value? |
| How to cut scoring into partitions and who takes which | agent (capabilities, online windows, GPU memory) |
| Which lane each key-holding node fetches | agent or static policy |
| Whether to spend budget on a backfill versus the daily delta | agent proposes, owner gates above a threshold |
| Changing a slice boundary or a partition layout | owner (schema-level) |
| Anything that adds a command to the allowlist | owner, via ADR |

## Protocol candidates

| Protocol | Sketch | Concern |
|---|---|---|
| Single planner per epoch | The node that first publishes a heartbeat in an epoch (or lowest id online) writes the plan; others follow or amend within a window. | Planner sleeps mid-epoch; amendment rules. |
| Proposal and quiet acceptance | Any node may publish a proposal; after a window without a conflicting proposal it stands; conflicts resolved by a fixed rule (most specific, then lowest id). | Windows versus Drive latency; many proposals. |
| Capability auction | Units are published; nodes bid with a cost estimate from their capabilities; lowest bid owns. | Needs good cost models; gaming is not a concern (one owner) but drift is. |
| No negotiation | Policy assigns deterministically from capabilities; agents only explain and flag anomalies. | Loses the "agents agree" intent; simplest and safest. |

## Cost and safety

- Every agent turn costs model budget; planning should be cheap relative to
  the work it plans (RN2 in spirit).
- Agents read the ledger and produce proposals; they never call the registry
  directly. The registry remains the only path to effects (G5).
- A proposal that exceeds a budget envelope is invalid before any claim (S5).
- The owner can pin a plan or veto; defaults conservative (RF7).

## What we need to learn

- Whether deterministic policy already covers most daily planning, leaving
  agents for exceptions (backfills, failures, new devices). If so, the
  protocol can start as "single planner per epoch, agent explains" and grow.
- How the owner wants to see and approve proposals (a file in the store, a
  notification, a CLI command).

## Target resolution

The decision surface table filled in by the owner, and one protocol chosen for
the first spike, by the end of session 1.
