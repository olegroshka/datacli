---
id: SESSION-001
title: Brainstorm session 1: goals, requirements and the two shaping questions
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.3
depends_on: [KB-001, KB-002, OQ-001, OQ-003, SESSION-001-PREP]
referenced_by: [INV-001]
---

# Session 1 plan

Purpose: turn the owner's intent into goals the owner signs, and narrow the
two questions that shape everything else. Stay abstract; park implementation.

## Pre-reads (15 minutes)

- KB-001 sections 1 to 4 (vision, actors, goals, non-goals).
- KB-002 section 2 (what is single-node today).
- OQ-001 and OQ-003 option tables.

## Agenda (about 90 minutes)

| Slot | Topic | Output |
|---|---|---|
| 10 min | Restate the problem in the owner's words; correct KB-001 section 1. | Agreed vision sentence. |
| 20 min | Goals: keep, amend, drop, add. Rank the top three. | KB-001 section 3 signed; confidence tags resolved to firm or dropped. |
| 10 min | Non-goals: what we will deliberately not build. | KB-001 section 4 signed. |
| 10 min | Layering principle (KB-001 3a): consensus, negotiation, policy as separate layers; the git-ledger quality bar. | Signed or amended. |
| 15 min | OQ-001: confirm or reject the git-ledger leaning; static assignment of paid work; the compare-and-set spike. | Leaning confirmed or replaced; spike defined. |
| 15 min | OQ-003 and OQ-005: decision surface, first protocol, provider tiers and caps, what runs headless where. | Tables filled; protocol and tiers for the spike. |
| 10 min | Scenarios: which of S1 to S7 matter most; add the owner's own. | Ranked scenario list. |

## Questions to put to the owner, in order

1. Which devices, with which GPUs and which typical online hours? Which hold
   the EODHD key and a Drive token today? (Feeds RF1 and S2.)
2. What work hurts most on one node today: scoring latency, fetch duration,
   or something else? (Ranks G1 versus G2.)
3. Is a small always-on box at home or the cheapest cloud tier acceptable, or
   is "no server" absolute? (Decides the shape of OQ-001.)
4. For fetching, is a fixed split by lane per device acceptable, with agents
   deciding only the GPU work? (Could remove the hardest consistency problem.)
5. How should agents ask for approval: a file in Drive, a notification, a
   command you run? How often do you want to be asked at all?
6. Must the fleet keep scores current daily, and what model budget per day is
   acceptable? (Connects to the open scoring-currency decision.)
7. Any device you do not trust with the API key or the token?
8. Which subscriptions and harnesses exist on which devices, and are their
   headless modes and rate windows acceptable for unattended epochs? (OQ-005)
9. What is the cap on agent turns per epoch you would accept before the
   fleet falls back to deterministic policy? (RN6)

## Facilitation rules

- Abstract first: if a sentence names a library, a file format or a protocol
  implementation, it goes to the parking lot.
- Every goal gets a measurable success candidate or it is a wish, not a goal.
- Disagreements become OQ options with criteria, not arguments.
- Nothing in the single-node substrate is weakened to make the fleet easier;
  such a need becomes an ADR request.
- Consensus and negotiation are discussed as separate layers; a proposal that
  merges them is a design smell to be named, not adopted.
- The git-ledger design is not sketched in the session; it gets its own DD
  under the quality bar.

## Outcomes so far (2026-09-13)

The session ran as three design rounds in conversation, recorded in
`SESSION-001-prep.md` section 10. Of the nine owner questions: 1 (devices)
answered in KB-002 section 4; 3 (no server) answered by D11, a hosted git
repository; 4 (fixed split by lane) answered yes in principle (P2, pending);
5 (approval) answered by D7, two tiers; 6 (scoring currency) answered by D2,
D8 and D9, the rebench then a one-year pass with embeddings; 8 (harnesses)
partly answered by D3 and D13. Still open: 2 (what hurts most, implicitly
scoring), 7 (untrusted devices), 9 (turn caps). KB-001 goals are not yet
signed; the amendments proposed in SESSION-001-PREP section 4 await the
owner.

## Parking lot

- Local bare git repository as the "real" ledger in CI.
- `git log` of the ledger repository as the first fleet audit view.
- `drive.file` visibility across devices sharing one OAuth client: verify
  before ADR-003.
- Price data hygiene remains the blocker for absolute signal claims; the
  median market proxy is accepted for ranking configs only (D2).
- A systemd adapter contract for the Linux node (OQ-006, Option B).

## Outputs and next steps

- KB-001 to version 0.2 with owner amendments; status remains DRAFT until the
  OQs have recommendations.
- OQ-001 and OQ-003 narrowed, each with a spike plan and a target date.
- Schedule session 2 for OQ-002 (units) and OQ-004 (data plane), which depend
  on the outcomes above.
- Only after sessions 1 and 2: start DD-001 and the two-node in-process
  simulation as the first code.
