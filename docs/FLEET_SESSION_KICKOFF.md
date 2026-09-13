# Fleet initiative: kickoff for the next session

State as of 2026-09-13. The single-node work is done and stable (see
`docs/SCHEDULER_DAILY_RUN_RECOVERY.md`); this session is about the next
initiative only: datacli as a fleet of the owner's devices.

## Copy/paste prompt for the new session

```text
You are continuing the datacli fleet initiative in
C:\Users\olegr\PycharmProjects\datacli. Read docs/FLEET_SESSION_KICKOFF.md,
then docs/fleet-initiative/README.md and follow its warm-up order: KB-001
(charter, note section 3a), KB-002 (section 4 has the devices), GLOSSARY,
then SESSION-001-prep.md sections 6 and 10 (the revised trajectory and the
decisions D1 to D13 of 2026-09-13), then the OQ being discussed. Also read
AGENTS.md for the house rules.

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

- `docs/fleet-initiative/` holds the substrate: README, KB-001 (charter,
  goals G1 to G10, section 3a, scenarios S1 to S9; not yet signed), KB-002
  v0.2 (today's node, code-grounded corrections, devices in section 4),
  GLOSSARY v0.3, INV-001 v0.4, OQ-001 to OQ-006, SESSION-001 (agenda and
  outcomes), and SESSION-001-prep.md v0.2 (code findings F1 to F22, proposals
  P1 to P15, the revised trajectory in section 6, the agreed high-level
  design and decisions D1 to D13 in section 10).
- Decided on 2026-09-13 (D1 to D13): three nodes (this Windows box as pinned
  fetcher and console; a Linux RTX 5090 32 GB box as primary scorer with a
  full mirror and a batched local endpoint; a Legion RTX 5090 24 GB laptop
  that scores only when docked); private git repository as the ledger with a
  deploy key per device; two-tier approval; rebench on the Linux box as the
  first fleet unit; embeddings and a one-year pass afterwards; cron for the
  Linux tick; no agent turn in steady state.
- Still proposals, not decisions: P1 to P15 in SESSION-001-prep.md section 3
  (epoch as UTC date, pinned versus claimable units, no heartbeat record, the
  two-operation Ledger port, no budget counter in v1, the Planner port, output
  descriptors, write scope, policy on a protected ref). KB-001 goal
  amendments in section 4 of the same file.

## What the next session does, in order (SESSION-001-prep.md section 6)

1. Sign or amend KB-001 goals; accept, amend or reject P1 to P15.
2. Phase 1a (scoring stream, separate change): runner concurrency; "local"
   defined by node-declared endpoints; quantisation in the model id.
3. Phase 1b: the S4U git fetch spike (empty private repository, one deploy
   key, a harmless logged-off task).
4. Phase 1c: DD-001 with the Ledger port (`read`, `cas`) and the Planner
   port; INV-004 seeds (prep section 7); ADR-001 and ADR-004 PROPOSED.
5. Phase 2: the N-node in-process simulation over a fake ledger.
6. Phase 3: DD-002 and the git ledger over a local bare repository, then the
   two-device race spike; only then ADR-001 ACCEPTED.

## Facts that will matter

- Devices are recorded in KB-002 section 4. Not yet provisioned: deploy keys,
  a Drive token on the Linux box, the mirror seed.
- The daily job runs logged-off (S4U) on the current node; Drive token and
  API key were proven visible from that session. Subscription harness
  credentials from a logged-off session are unverified.
- datacli already exposes an MCP server; harnesses should reach a node only
  through it and the command registry.
- The scoring stream (`score run`) is deferred from scheduling by INV-002
  (budget). Scoring currency was decided on 2026-09-13: the rebench on the
  Linux box, then a one-year pass with embeddings, through the fleet (D2, D8,
  D9). Scoring outputs are already day partitions; only the per-directory
  `state.csv` is shared and must become node-local.
- The price data hygiene blocker stands for absolute signal claims; the median
  market proxy is accepted for ranking configurations only.
