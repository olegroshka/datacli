---
id: OQ-005
title: The agentic plane: which models, through which harnesses, in which roles, at what cost?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.2
sources:
  - KB-001 G10, RF10, RF11, RN6, section 3a, scenarios S8, S9
  - Owner discussion 2026-09-12 (Claude and OpenAI subscriptions; avoid extra cost; home boxes as extra hands)
  - Current practice: orchestrator plus narrow workers, durable artifact-based communication, model-agnostic harnesses over MCP
depends_on: [KB-001, GLOSSARY, OQ-003]
referenced_by: [INV-001, OQ-003]
---

# OQ-005 - The agentic plane

## The question

The owner has Claude and OpenAI subscriptions and several GPU devices, wants
no extra recurring cost, and wants capable agents in every node. Which model
access is used for which task, through which harness, in which role, and how is
spend bounded?

## Provider tiers (to confirm)

| Tier | Where it runs | Suited to | Cost | Caveats to verify |
|---|---|---|---|---|
| Local open-weight | A node's own GPU (vLLM, Ollama or similar) | Embeddings, classification, first-pass triage, bulk summarisation, the scoring stream's model work | Electricity | Quality per task; VRAM per device; model licensing |
| Subscription harness | Claude Code or Codex in headless mode on a signed-in device | Planning, hard reasoning, failure triage, review | Already paid | Rate windows for sustained unattended use; whether stored credentials work from a logged-off (S4U) session; terms of use for automation |
| Pay-per-token API | Any node | Nothing by default | Per call | Only under an explicit budget envelope set by the owner |

Policy selects the tier per task class and falls back **up** a tier on quality
failure and **down** to deterministic policy on cap exhaustion, never to
silence (RN6).

## Owner decisions (2026-09-13, D3 and D13)

- The local tier is a batched OpenAI-compatible endpoint on the node (vLLM or
  equivalent) with Ollama as the fallback. Policy declares each node's local
  endpoints and the model ids they serve; "local" and zero cost follow from
  that declaration, not from the `ollama/` model-id prefix used today
  (`llm/tiers.py`). Quantisation is part of the model id because it changes
  results.
- The scoring runner keeps N requests in flight (D4) so batched serving pays;
  throughput is recorded as articles per hour at the run's concurrency and
  becomes the node's advertised capability for lease sizing (P15).
- Harness for the planner and verifier roles: Claude Code headless on the
  Windows box under the subscription, or a local model on the Linux box, with
  the same role prompt. The MCP server today is read-only (three tools); the
  agent needs only ledger read and record write, so no mutating MCP surface is
  required in v1.
- Steady state has no agent turn (D13); pay-per-token stays at zero by
  default (G10).

## Harness boundary

A harness reaches a node only through the node's MCP tool surface and the
command registry (RF11); the agent has no other path to effects. The harness is
swappable: the same role prompts and ledger records must work whether the
agent runs on a local model or a subscription harness.

## Roles as assignments

Planner, worker, verifier, scout (GLOSSARY). A worker needs no model at all. A
node takes roles for an epoch according to capability and availability; the
owner may pin roles to devices.

## Interaction style

Asynchronous, through addressed inbox records in the ledger: read, act, write,
sleep. No live agent-to-agent sessions in the first release; a conversation
that needs several turns is several records, each resumable after a reboot.
This is what makes "complex agentic interactions" compatible with G4.

## Cost bounding

- Turn caps per role per epoch (RN6), configured in policy.
- Planning spend must be small relative to the work it plans.
- Zero pay-per-token spend by default (G10); an envelope is an explicit owner
  action recorded in the ledger.

## What we need to learn

- Which devices carry which subscriptions and harnesses, and their headless
  and rate-window behaviour under unattended use.
- Whether the harness credentials are usable from a logged-off session, as the
  Drive token proved to be.
- Which local models on the owner's GPUs clear the quality bar for the bulk
  tasks, measured on the existing scoring benchmarks.
- The right shape of a role prompt so it is portable across harnesses.

## Target resolution

Tiers, caps and the harness boundary confirmed in session 1; ADR-004 after the
subscription and local-model facts are measured.
