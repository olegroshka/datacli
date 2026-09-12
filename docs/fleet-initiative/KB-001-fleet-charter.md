---
id: KB-001
title: Fleet initiative charter: intent, goals, requirements, scenarios
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-12
version: 0.2
sources:
  - Owner's statement of intent, 2026-09-12 ("run datacli on my different devices with powerful GPUs, agents inside each node agree on slicing the problem and work on it together")
  - docs/scheduler-initiative/KB-001 (single-node intent this extends)
depends_on: [GLOSSARY]
referenced_by: [BOOTSTRAP-FLEET, INV-001, OQ-001, OQ-002, OQ-003, OQ-004]
---

# KB-001 - Fleet charter (draft for brainstorm session 1)

Everything below is a proposal to be argued with. Items carry a confidence
tag: **firm** (the owner stated it), **likely** (follows from firm items),
**guess** (author's inference, needs the owner).

## 1. Vision

One dataset, many hands. The owner's devices form a fleet of datacli nodes that
together keep the whole EODHD data estate and its derived products (indexes,
news-derived scores and embeddings) current every day, using whichever
machines are awake and whichever GPUs are free, with no machine being special
and no server to babysit. The owner talks to the fleet as one system; the fleet
shows its work per node.

## 2. Actors

| Actor | Description |
|---|---|
| Owner | One human; the only principal; approves policy and budgets. |
| Node | One datacli installation on one device: repo, venv, data root, local scheduler, local agent. Has a stable identity and advertised capabilities (GPU, API key present, bandwidth, typical online window). |
| Planner agent | An AI agent inside a node that proposes how to cut work and can negotiate with peers. Proposes; never executes outside the registry. |
| Worker | The node's existing execution substrate (registry, runner, journal) doing one claimed unit of work. |
| Rendezvous store | The shared data plane nodes use to exchange data and coordination records. Google Drive today. |
| Provider(s) | EODHD (paid, quota-bound), model services or local models (paid or GPU-bound). |

## 3. Goals (draft)

| Id | Goal | Why | Candidate success measure | Confidence |
|---|---|---|---|---|
| G1 | Scoring and embedding throughput scales with the number of GPU nodes online. | This is the compute the single node cannot do daily; the owner's GPUs are idle. | Full corpus re-score wall time falls roughly in proportion to nodes; a daily incremental score finishes inside the refresh window on any single GPU node. | firm |
| G2 | Fetch work is sliced so that no paid call is made twice for the same slice on the same day, by any node. | Quota and money; duplicated fetches are pure waste. | Zero duplicate (endpoint, slice, day) calls in the fleet ledger over a week. | firm |
| G3 | One shared, consistent data plane: every node sees the fleet's latest outputs, and the store holds exactly one current copy of every file. | The whole point of a shared dataset; the Drive duplicate incident shows the cost of getting this wrong. | Reconcile reports zero duplicates and zero diverged copies across all nodes' pushes. | firm |
| G4 | No always-on coordinator. Any node may be off for days; the fleet still makes progress with whoever is online. | These are personal devices that sleep, reboot and travel. | A node absent for a week rejoins without manual repair; work it held is re-claimed within one cycle of its lease expiring. | firm |
| G5 | Agents decide the *plan*, the substrate does the *work*. Planning is negotiable; execution stays allowlisted, journaled and locked exactly as on one node. | Safety boundary already accepted for the scheduler; agents must not widen it. | Every executed unit maps to a registry command and a journal record; no agent can cause a paid call that is not a claimed, allowlisted unit. | firm |
| G6 | The owner can see the fleet as one system and each node as three honest planes. | Three-plane honesty is the scheduler's core invariant; a fleet must not blur it. | A fleet view lists every node's desired, backend and execution state plus its claims, without inferring one from another. | likely |
| G7 | Budgets are global: API quota, model spend and Drive bandwidth are capped for the fleet, not per node. | One bill, one quota. | Fleet spend never exceeds the cap even when nodes cannot talk to each other for a while. | likely |
| G8 | Single-node mode keeps working unchanged. A fleet of one is just today's datacli. | Incremental adoption; no big-bang migration. | Every current test passes with fleet features off; the daily job is unaffected. | firm |
| G9 | No secret ever crosses between devices. Each node holds its own API key and Drive token. | Security hygiene already in force. | Coordination records contain no secret material; a node with no key simply cannot claim fetch work. | firm |
| G10 | A flexible agentic plane: a node's agent runs on a local model, on a subscription harness (Claude, OpenAI) or, only under an explicit budget, on a pay-per-token API, chosen per task class by policy. The daily steady state needs no model call at all. | Avoid extra cost; use what is already paid for; the fleet must stay useful with no model available. | Zero pay-per-token spend by default; a full daily cycle completes with model calls disabled. | firm |

## 3a. Layering principle (firm)

Consensus, negotiation and policy are **separate abstract layers with
separate contracts**. This is the owner's explicit requirement and the
single most important structural decision of the initiative.

| Layer | Owns | Nature | Must hold |
|---|---|---|---|
| Consensus | The mechanical truth about who owns what: claims, leases, tie-breaks, completions, budget counters, node liveness. | Deterministic, model-free, small record schema, provable against a fake medium. | Correct with zero agents present. Every claim is a compare-and-set on the ledger; there is no other way to obtain ownership. |
| Negotiation | How agents propose, critique, amend and accept plans; role assignment; exception handling (backfills, failures, new devices). | Model-driven, bounded in turns and spend, resumable, asynchronous through addressed ledger records. | Its output is only ever an *input* to the consensus layer. A negotiation outcome becomes ownership only by passing through a consensus claim. |
| Policy | The owner's caps, gates, defaults and vetoes; provider tiers per task class. | Static configuration plus explicit owner actions. | Sits above both layers; neither layer can loosen it. |

Corollaries: the ledger medium implements the consensus layer's port and
nothing else; the negotiation layer can be switched off and the fleet still
runs on deterministic policy; the two layers are designed, tested and
decided in separate artefacts (ADR/DD per layer).

**Quality bar for the ledger implementation.** The leaning is a git-based
ledger (OQ-001). Its design is held to an ultra-high standard: tight,
minimal semantics (what a ref means, what a record is, what a push proves),
a small versioned record schema, explicit retention and compaction,
exhaustive adversarial tests over a fake and a real git remote, and its own
ADR and DD reviewed by the owner before a line of product code. Clean and
small beats featureful.

## 4. Non-goals (draft)

- Not a general-purpose job scheduler or a cloud cluster; the fleet is the
  owner's own devices.
- Not multi-tenant; one owner, one account, one trust domain.
- Not real-time; daily and intra-day batch cadence only.
- Not exactly-once by construction; at-least-once with idempotent writes is
  acceptable where the cost of a repeat is a cheap local build, never where the
  repeat is a paid call (that is G2).
- Not a replacement for the store: Drive (or a successor) stays the data plane;
  the fleet does not invent a database for the market data itself.

## 5. Requirements (draft)

Functional:

| Id | Requirement | Confidence |
|---|---|---|
| RF1 | A node has a stable identity and can advertise capabilities (GPU class, has EODHD key, has Drive token, bandwidth class, typical online window). | likely |
| RF2 | Work is expressed as units over named slices (lane x dataset x ticker range or day range; corpus partition for scoring) with an idempotency key. | likely |
| RF3 | A node claims a unit with a lease; a lease expires; an expired lease may be re-claimed; a completed unit is marked done with a pointer to its output. | likely |
| RF4 | Outputs are written so that two nodes never write the same remote file: partition ownership follows slice ownership. | firm (from G3) |
| RF5 | Every node can pull the fleet's current outputs it needs as inputs (scoring needs the news partitions another node fetched). Sync becomes bidirectional. | likely |
| RF6 | A planner agent can read the fleet's state and propose a decomposition; a proposal is a record other nodes can accept, amend or ignore under a fixed protocol. | firm |
| RF7 | The owner can veto or pin any plan and set budgets; defaults are conservative. | likely |
| RF8 | Fleet-level observability: per node the three planes plus claims; per unit its lifecycle. | likely |
| RF9 | Consensus, negotiation and policy have separate ports and are testable independently; negotiation can be disabled without touching consensus. | firm |
| RF10 | Roles (planner, worker, verifier, scout) are assignments an online node takes for an epoch, not fixed machines. | likely |
| RF11 | Agent harnesses reach a node only through its MCP tool surface and the registry; a provider adapter selects the model tier per task class. | likely |

Non-functional:

| Id | Requirement | Confidence |
|---|---|---|
| RN1 | Coordination tolerates partitions and absent nodes for days without a human. | firm |
| RN2 | Coordination traffic is small next to the data (Drive API quota is finite). | likely |
| RN3 | Code version skew between nodes is detected and bounded (contract versions already exist). | likely |
| RN4 | Everything is testable with fakes: an in-process simulation of N nodes over a local store backend. | firm (house rule) |
| RN5 | Failure is loud: a fleet problem surfaces in the owner's notification channel, not in Drive. | firm (lesson learned) |
| RN6 | Model calls are capped per epoch and per role; exhausting a cap degrades to deterministic policy, never to silence. | firm |
| RN7 | The git-ledger implementation meets the quality bar in section 3a before it carries a single paid claim. | firm |

## 6. Candidate invariants

1. A paid (endpoint, slice, day) is called by at most one node. (G2)
2. One writer per remote file, ever. (G3)
3. Any node may disappear forever without blocking the fleet. (G4)
4. Agents propose; only the registry executes. (G5)
5. Each unit of work has exactly one owner at a time and a journal record on
   that owner. (G6)
6. The fleet's spend is bounded by the owner's caps regardless of connectivity. (G7)
7. Fleet off means today's single node, byte for byte. (G8)
8. Coordination records are secret-free. (G9)
9. Negotiation never bypasses consensus: no claim, lease or budget change
   originates from an agent turn directly. (3a)
10. The fleet completes a daily cycle with all model calls disabled. (G10)

## 7. Scenarios to design against

- S1 Three GPU nodes online, one corpus re-score: the corpus is cut into
  partitions, each node claims some, results land as disjoint files, one
  manifest describes the whole.
- S2 Daily fetch split by lane across two nodes with keys; a third node without
  a key only scores.
- S3 A node claims a slice, fetches half, then its owner closes the lid for a
  week. The lease expires; another node re-claims; partial output is either
  resumable or discarded deterministically.
- S4 Two nodes claim the same slice within seconds of each other (Drive
  eventual consistency). The protocol yields one owner; the loser does no paid
  work.
- S5 A planner agent proposes a cut that would exceed the daily quota; the
  proposal is rejected by policy before any claim exists.
- S6 The owner adds a fourth device: install, sign in, advertise; the fleet
  starts handing it work without any change on the other nodes.
- S7 Two nodes run different code versions; one produces an output schema the
  other cannot read. The fleet notices before the file is consumed.
- S8 The subscription harness hits its rate window mid-epoch. Planning
  degrades to deterministic policy, the epoch completes, the owner is told.
- S9 A planner proposes a cut; the verifier's critique (a ledger record) points
  at a budget breach; the amended proposal is accepted; only then do claims
  appear. No paid call happened during the exchange.

## 8. Success criteria for the initiative (draft)

- The owner can state the goals above as their own, amended, and sign them.
- OQ-001 to OQ-004 each have a recommended option with evidence, ready for
  ADRs.
- A DD-001 exists that an implementer could build a two-node simulation from.
- Nothing in the single-node substrate had to be weakened.

## 9. Questions the owner must answer first

See `SESSION-001-brainstorm-plan.md`. The two that shape everything else: what
is the coordination medium (OQ-001), and how much do agents decide versus
policy (OQ-003). The agentic plane itself (providers, harnesses, roles,
caps) is OQ-005. Current leanings from the 2026-09-12 discussion are recorded
in each OQ under "Leaning"; they are not decisions.
