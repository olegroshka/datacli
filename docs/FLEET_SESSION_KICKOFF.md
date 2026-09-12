# Fleet initiative: kickoff for the next session

State as of 2026-09-12. The single-node work is done and stable (see
`docs/SCHEDULER_DAILY_RUN_RECOVERY.md`); this session is about the next
initiative only: datacli as a fleet of the owner's devices.

## Copy/paste prompt for the new session

```text
You are continuing the datacli fleet initiative in
C:\Users\olegr\PycharmProjects\datacli. Read docs/FLEET_SESSION_KICKOFF.md,
then docs/fleet-initiative/README.md and follow its warm-up order: KB-001
(charter, note section 3a), KB-002, GLOSSARY, then the OQ being discussed.
Also read AGENTS.md for the house rules.

This is a substrate session: goals, requirements, contracts and decision
records. No product code until DD-001 (consensus layer) exists and the owner
has accepted KB-001. When code starts, the first artefact is an in-process
simulation of N nodes over fake media; nothing touches the real Drive tree,
the real daily job, real subscriptions or a real git remote without my
explicit OK.

Hard rules for this initiative:
- Consensus, negotiation and policy are separate abstract layers with
  separate contracts and separate ADR/DD artefacts. Never merge them.
- The git-ledger implementation design (DD-002) must be ultra-high quality:
  tight, minimal semantics, small versioned record schema, retention and
  compaction rules, exhaustive adversarial tests, reviewed before any code.
- Zero pay-per-token model spend by default; subscriptions and local GPUs
  first; the daily cycle must complete with all model calls disabled.
- Nothing in the single-node substrate is weakened to make the fleet easier.

Never stage or commit .codex/, .claude/, AGENTS.md, or datacli.toml.
```

## Where things stand

- `docs/fleet-initiative/` holds the scaffolding at version 0.2: README,
  KB-001 (charter with goals G1 to G10, the section 3a layering principle,
  requirements, invariants, scenarios S1 to S9), KB-002 (today's node and its
  single-node assumptions), GLOSSARY, INV-001, OQ-001 to OQ-005, and
  SESSION-001 (the brainstorm agenda).
- Leanings recorded on 2026-09-12, none decided: git repository as the
  consensus ledger with Drive as the data plane (OQ-001); static per-lane
  assignment of paid fetch work, agents on GPU and exception work (OQ-002,
  OQ-003); single planner per epoch with verifier review, interactions as
  inbox records (OQ-003); provider tiers local, subscription, pay-per-token
  under explicit budget only (OQ-005).

## What the next session does, in order

1. Run SESSION-001: sign or amend KB-001 (goals, non-goals, section 3a),
   answer the nine owner questions, confirm or replace the leanings, rank the
   scenarios. Record outcomes in the artefacts, bump versions.
2. Write ADR-001 (ledger medium) as PROPOSED with the compare-and-set spike
   plan; write ADR-004 (provider policy) as PROPOSED.
3. Draft DD-001 (consensus layer records and ports) small enough to simulate:
   Node, Unit, Slice, Claim, Lease, Completion, BudgetCounter, Ledger port.
4. Only then draft DD-002 (git ledger) under the quality bar, and DD-003
   (negotiation records and caps).
5. INV-004 adversarial scenarios before any ADR is accepted: split brain,
   stale lease, double claim within seconds, version skew, quota race,
   harness rate-window exhaustion.
6. First code, when authorised: the N-node in-process simulation over a fake
   ledger and the local storage backend, exercising DD-001 only.

## Facts that will matter

- Devices, GPUs, online windows, which hold the EODHD key and a Drive token,
  which carry a Claude or OpenAI subscription: not yet recorded; ask first.
- The daily job runs logged-off (S4U) on the current node; Drive token and
  API key were proven visible from that session. Subscription harness
  credentials from a logged-off session are unverified.
- datacli already exposes an MCP server; harnesses should reach a node only
  through it and the command registry.
- The scoring stream (`score run`) is deferred from scheduling by INV-002
  (budget); scoring currency is an open owner decision recorded in
  `docs/SCHEDULER_DAILY_RUN_RECOVERY.md` section 6b.
