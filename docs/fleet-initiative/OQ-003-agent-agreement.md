---
id: OQ-003
title: The negotiation layer: what do agents decide, and how do they agree?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.3
sources:
  - KB-001 G5, G10, RF6, RF7, RF9, RN6, section 3a, scenarios S5, S8, S9
  - docs/scheduler-initiative/ADR-003 (allowlisted execution)
  - Owner discussion 2026-09-12 (collaborative team of agents; separate layers)
depends_on: [KB-001, GLOSSARY, OQ-002, OQ-005]
referenced_by: [INV-001]
---

# OQ-003 - Agent agreement (negotiation layer)

## Scope boundary

This question is about the **negotiation layer only** (KB-001 3a): how agents
in different nodes propose, critique and accept a plan. It never touches how
ownership is obtained; that is the consensus layer (OQ-001, OQ-002). A protocol
proposed here must be expressible as records that end with a request to the
consensus layer, and the fleet must run without this layer entirely.

## The question

The owner wants the nodes to form a collaborative team whose agents take part
in complex interactions to complete a problem. Which decisions are theirs,
which are policy, and what protocol turns several opinions into one plan
without a server and without runaway spend?

## The decision surface (owner to fill)

| Candidate decision | Agent, policy, or owner? |
|---|---|
| Which units exist today (pending work discovered from store and sidecars) | policy computes this; agents only explain and flag anomalies |
| How to cut GPU work into partitions and who takes which | agent (capabilities, online windows, GPU memory) |
| Which lane each key-holding node fetches | leaning: static policy per epoch; agent may propose a change at an epoch boundary |
| Backfill versus daily delta; spending against a budget envelope | agent proposes, owner gates above a threshold |
| Failure triage: what went wrong on a node and what to do | agent (scout and verifier roles), bounded turns |
| Changing a slice boundary or a partition layout | owner (schema-level) |
| Anything that adds a command to the allowlist | owner, via ADR |

## Protocol candidates

| Protocol | Sketch | Concern |
|---|---|---|
| Single planner per epoch, verifier review | The planner role (first online node of the epoch, or lowest id) writes the epoch plan as a record; a verifier role critiques within a window; the amended plan stands; claims follow. | Planner sleeps mid-epoch; window length versus medium latency. |
| Proposal and quiet acceptance | Any node may publish a proposal; after a window without a conflicting proposal it stands; conflicts resolved by a fixed rule. | Many proposals; windows versus latency. |
| Capability auction | Units are published; nodes bid with cost estimates from their capabilities; lowest bid takes the unit. | Needs honest cost models; drift over time. |
| No negotiation | Policy assigns deterministically; agents only explain. | Loses the collaborative intent; simplest and safest; also the mandatory fallback (RN6). |

## Leaning (2026-09-12, not a decision)

**Single planner per epoch with verifier review**, interactions carried as
addressed inbox records (question, critique, request, acceptance) so that a
node can sleep mid-conversation and resume. Complex interactions are reserved
for exceptions: failure triage, backfill negotiation, schema proposals, a new
device joining. The daily steady state runs on deterministic policy with no
agent turn (G10). Every role has a turn cap per epoch; exhaustion degrades to
policy (RN6).

## Owner decisions (2026-09-13, SESSION-001-PREP 10.4 and 10.5)

- D13: the daily steady state has no agent turn. Agents handle four things,
  all asynchronously through ledger records: proposing a pass (schema, model,
  quantisation, window, estimated GPU hours inside the envelope), the quality
  gate every N scored days (existing eval and panel tooling, critique record,
  pause on a collapsing signal), triage of exceptions the deterministic policy
  does not cover, and bench proposals before a pass is committed.
- D7: two-tier approval. Explicit owner approval, given on the Windows box,
  for starting or changing a pass, changing policy, and anything paid.
  Automatic from day one for every reversible action: reassign a day, pause on
  the quality gate, mark a node degraded, retry a pull.
- Decision surface: "which units exist" and "who takes which day" are policy
  (deterministic planner); "which pass to run" is agent-proposed and
  owner-approved; slice boundaries and allowlist changes stay with the owner.

Proposal P6 (a Planner port with `DeterministicPlanner` in v1 and
`AgentPlanner` later, both emitting the same `EpochPlan`) is the recommended
structure for RF9 and RN6; contracts follow in DD-003 after DD-001.

## Cost and safety

- Every agent turn costs subscription quota or GPU time; planning must be
  cheap relative to the work it plans.
- Agents read the ledger and write records; they never call the registry
  directly, and they never write consensus records (claims, leases) themselves.
  The consensus layer accepts a claim request only from the node's own
  deterministic worker, referencing an accepted plan.
- A proposal that exceeds a budget envelope is invalid before any claim (S5).
- The owner can pin a plan or veto; defaults conservative (RF7).

## What we need to learn

- Whether deterministic policy already covers most daily planning, so the
  first protocol can be minimal and grow only where agents add value.
- How the owner wants to see and approve proposals (a file in the ledger, a
  notification, a CLI command) and how often.
- The right turn caps per role, measured on a simulated fleet.

## Target resolution

Decision surface filled by the owner and one protocol chosen for the spike in
session 1; contracts in DD-003 after DD-001 exists.
