---
id: SESSION-001-PREP
title: Session 1 preparation: substrate critique, code findings, proposals and agenda
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.2
sources:
  - docs/FLEET_SESSION_KICKOFF.md
  - docs/fleet-initiative (all artefacts at v0.1/v0.2)
  - Source inspection of scheduler/, storage/, scoring/, eodhd/, llm/, mcp_server.py on 2026-09-12
depends_on: [KB-001, KB-002, GLOSSARY, OQ-001, OQ-002, OQ-003, OQ-004, OQ-005, OQ-006, SESSION-001]
referenced_by: [INV-001]
---

# Session 1 preparation

Purpose: bring the substrate from "frames the problem" to "a descriptor the
owner can sign and an implementer can build from". Section 2 is fact
(code-grounded, reverse-propagated into KB-002 v0.2). Sections 3 to 5 and 7 to
9 are the author's proposals from 2026-09-12. Section 10 records the design
rounds of 2026-09-13 and the owner's decisions D1 to D13; section 6 is the
trajectory as revised by those decisions. Proposals P1 to P15 that section 10
does not name remain proposals.

## 1. Verdict on the v0.2 substrate

What is already right and should not be reopened:

- The three-layer split (consensus, negotiation, policy) and the rule that
  negotiation output is only ever an input to consensus.
- The ledger quality bar and the demand for a fake medium plus a real one.
- "Fleet off is today's node, byte for byte" (G8) and "no secret crosses
  devices" (G9).
- Static assignment of paid fetch work per key-holding device (OQ-002
  leaning). This one fact removes most of the hard consistency problem.

Where v0.2 is still too loose to build from:

| # | Problem | Consequence if left |
|---|---|---|
| V1 | "Epoch" has no definition (calendar day? trading day? whose clock?). | Every record key is undefined. |
| V2 | The ledger is asked to carry four record classes with different churn and stakes: ownership (claims, leases), liveness (heartbeats), completions, and conversation (proposals, inbox). | A medium chosen for atomic ownership (git) is wrong for high-churn heartbeats; the ledger grows without bound and DD-002 cannot be small. |
| V3 | "Every claim is a compare-and-set" (3a) contradicts "static assignment of paid lanes" (OQ-002 leaning) unless the two are reconciled explicitly. | Either the invariant or the leaning gets silently dropped in DD-001. |
| V4 | A `BudgetCounter` is in the DD-001 shopping list, but with static paid assignment and zero model spend by default there is nothing dynamic to count in v1. | Over-building consensus before it has a user. |
| V5 | Four roles (planner, worker, verifier, scout) and a negotiation protocol are specified before a single unit has been executed by two nodes. | The first code has to satisfy contracts that have never been exercised. |
| V6 | KB-002 says scores and embeddings are single snapshot files. The code says they are day partitions with per-day state (section 2). | OQ-002 and OQ-004 are solving a problem that does not exist for the first workload. |
| V7 | Data-plane pull is required (RF5) but the sync layer has no download path, no write scope, and one manifest per (root, backend). | OQ-004 is under-specified relative to what code must change. |
| V8 | Node identity, clock skew between devices, and version skew are named but have no proposed mechanism. | Three adversarial scenarios without a home. |
| V9 | G7 (global budgets) is "likely" but its measure assumes a shared counter that no leaning provides. | An unmeasurable goal. |

## 2. Code-grounded findings (reverse-propagate into KB-002 v0.2)

Each row names the artefact the fact changes.

| # | Fact (file:line) | Changes |
|---|---|---|
| F1 | Scoring output is already partitioned by publication day with a per-day state file: `news/scores/<schema>@<v>/<backend-id>/YYYY-MM-DD.parquet` + `state.csv`, embeddings likewise (`scoring/store.py:1-16`). Partitions are upserted on `(article_id, symbol)` and written atomically. | KB-002 section 2 last row is wrong; delete it. The scoring unit `(schema@v, backend/model, day)` and its one-writer file exist today. OQ-002 sub-question 1 (scoring) is answered by code. |
| F2 | Scoring provenance columns include schema, schema_version, backend, model, prompt_hash (`scoring/store.py:47-62`); schemas are versioned TOML (`scoring/schemas/event_v1..v5.toml`). | Version skew for scoring outputs (S7) is detectable from the row itself; the unit key must include schema version and model id. |
| F3 | The push planner has no notion of which paths a node may write; include globs and excluded dir names only (`storage/engine.py:45-46,96`). Orphans are reported, never deleted; trash only via `sync reconcile --run --trash-duplicates`. | OQ-004 sub-question 1: ownership enforcement is a new `write scope` in `build_plan`, not a convention. |
| F4 | Manifest is `<data_root>/.sync/<backend>.json`, version 1, entries `size, mtime_ns, md5, remote_id, uploaded_at` (`storage/engine.py:40,214`). | A completion record can carry exactly these fields as an output descriptor; the manifest entry is already the right shape. |
| F5 | No download, pull, ETag or conditional-write path exists anywhere in `storage/`. `StorageBackend` requires `describe, ensure_auth, upload`; optional `list_remote, trash` (`storage/backends.py:58-95`). | RF5 needs one new backend method (`download(remote_id, local)`) and one new engine path (pull by descriptor). Drive supports it; local backend trivially. |
| F6 | Drive folder creation is list-then-create and not atomic (`storage/gdrive.py:218`); `drive.file` scope only sees files the OAuth app created (`storage/gdrive.py:8,221`). | Two nodes creating the same folder concurrently can fork the tree. Whether node B can see node A's files depends on both using the same OAuth client; this is a spike item, not a known fact. |
| F7 | Node identity today is `profile_id`, a UUID bound to (repo_root, config_path) and never derived from paths (`scheduler/store.py:77-186`). No machine id exists in any record. | `node_id` can be the profile id plus a human alias; nothing new to invent. |
| F8 | Locks are OS-held byte-range locks with no lease or TTL (`scheduler/locks.py:48-134`); crash release is automatic. | The fleet lease is the first time-based ownership concept in the codebase. Keep it in the consensus layer only; never let it leak into the lock manager. |
| F9 | `Capability` has `mutation, network, requires_run` but no `paid` or `model` flag; INV-002's CORE/OPTIONAL/DEFER/FORBID classes live only in the doc (`scheduler/commands.py:45-56,124-133`). Admission is table membership. | A fleet unit needs `paid` and `model` on the capability; `score run` must be admitted (currently DEFER) before it can be a unit. INV-002 (scheduler) gets an amendment, not a fork. |
| F10 | Contract versions exist: `COMMAND_CONTRACT_VERSION = "scheduler-commands-v1"`, `SCHEMA_VERSION = 1` (`scheduler/model.py:15-16`), `RUNNER_VERSION` (`scheduler/runner.py:34`), manifest version 1, scoring schema@v. | Node advertisement carries these; a unit names the ones it requires. RN3 has a mechanism. |
| F11 | Notification path exists: `LAST_RUN.txt`, `LAST_FAILURE.txt`, `[scheduler] notify_command` with `notify_on` (`scheduler/notify.py:56-159`). | RN5 routes fleet failures through the same channel; no new notifier. |
| F12 | Fakes exist for every port: `FakeBackend` (`scheduler/backends/base.py:44`), `LocalBackend`, fake awake clock and power (`tests/test_scheduler_runtime.py:28-52`), scripted registry results (`tests/test_scheduler.py:306`). | The N-node simulation grows from these; the missing fake is the ledger. |
| F13 | Model tiers already exist as names (`local, cheap, mid, strong, embed-local, embed-cheap`) mapped to ids with `is_local` (`llm/tiers.py`); scoring refuses paid models when `budget_usd = 0`. | OQ-005's tier table is half-implemented; the policy record can reference tier names. |
| F14 | Git 2.52 is installed; no git library is locked (`uv.lock`). | The git ledger can be a subprocess adapter; a local bare repository gives real compare-and-set semantics in tests without network. |
| F15 | `score run` already selects by day (`--days/--since/--until`), samples, caps, resumes per chunk from `state.csv`, and refuses paid models at `budget_usd = 0` (`scoring/cli.py:67-131`, `scoring/runner.py:88-106,171-244`). No symbol-range selector exists. | The scoring unit needs no new selector. Day is the only partition axis; do not invent a symbol axis. |
| F16 | The scoring `state.csv` is one file per `<schema>@<v>/<backend-id>` directory, rewritten after every chunk, shared by all days (`scoring/store.py:98,371`). | Two nodes scoring different days of one schema would both write it: a one-writer violation. It must become node-local (excluded from the write scope, never pushed); the completion record carries the state row; a consumer rebuilds status from partitions (`partition_status_counts`, `store.py:325`). |
| F17 | Scoring's universe filter reads the lanes' `prices_fetch_state.csv` sidecars (`scoring/select.py:30-129`). | A scoring node's pull set is the day's `news/articles/<day>.parquet` plus the lane price-state sidecars, not articles alone. |
| F18 | Embeddings and local models go through LiteLLM to an Ollama server; no torch or CUDA code exists; device choice is Ollama's (`llm/models.py:172-291`). | A node's GPU capability is "which Ollama models are pulled and answer", measurable by a probe; not a GPU class label. RF1 should say so. |
| F19 | Fetchers accept `--tickers` and `--limit` but no range or offset; `refresh` forwards them only to prices, dividends and splits (`eodhd/cli.py:479-487`). State sidecars are whole-lane CSVs. | Sub-lane fetch slicing is possible but would need sidecar partitioning; with pinned lanes (P2) it is unnecessary in v1. |
| F20 | News derived tables (`news_symbol_daily`, `news_issuer_daily`, `issuer_map`) are single-file snapshots rebuilt locally from the article partitions by DuckDB (`build_news_symbol_daily.py:79-195`). | Local builds are not fleet units: every consuming node rebuilds its own from pulled partitions. Same for `reindex`. |
| F21 | The MCP server exposes three read-only tools (`sql`, `describe_schema`, `list_lanes`) over stdio, no mutation, no auth (`mcp_server.py:97-119`). | An `AgentPlanner` needs only ledger read and record write; no MCP mutation surface is required in v1. RF11 stays true by construction. |
| F22 | Every `refresh --run` ends with `status --write`, producing `<data_root>/STATUS.json` with per-dataset freshness, behind, quiet and retired counts (`eodhd/status_eodhd.py:205-225,784-799`). | The v1 fleet view can be assembled from each node's pushed STATUS.json plus the ledger snapshot (G6 amendment). |

## 3. Proposals to sharpen the descriptor

Each is a candidate decision for the session. Accepting one edits the named
artefact.

**P1 Epoch is a UTC calendar date.** Epoch id = `YYYY-MM-DD`. Every ledger
record is keyed `(epoch, ...)`. A unit whose input day is D belongs to epoch
max(D+1, today) so that late-arriving news still gets one owner. (GLOSSARY,
DD-001.)

**P2 Two ownership regimes, one invariant.** Units are either *pinned* or
*claimable*.
- Pinned: paid fetch lanes, assigned per key-holding node by policy. At epoch
  start the pinned node writes its claim through the same compare-and-set path;
  it is uncontended by construction, so the invariant "no ownership without a
  claim record" holds and G2 holds by policy, not by racing.
- Claimable: idempotent GPU work (scoring partitions), claimed dynamically with
  a lease. A double claim within the medium's window costs GPU minutes, never
  money.
- Failover of a pinned lane in v1 is an owner action (re-pin in policy), not a
  consensus decision. This resolves V3 and drops the hardest S3/S4 cases from
  the first release without weakening G2. (KB-001 3a corollary, OQ-002.)

**P3 No heartbeat record.** Liveness is derived from lease freshness: a node
renews the leases it holds by a compare-and-set update of the claim record. A
node with no claims has no liveness to prove. This removes the highest-churn
record class from the ledger and makes DD-002 small. (GLOSSARY "Heartbeat"
becomes "Lease renewal"; OQ-001 spike list.)

**P4 The Ledger port is two operations.**
```text
read(ref) -> (head, snapshot)                      # snapshot = current records, no replay
cas(ref, expected_head, delta) -> committed(head') | conflict(head)
```
State is the tree at head; history is audit only. Fake ledger (in-process
dict), local bare git repository (real semantics, no network) and hosted git
(the product) all implement this port and must pass one shared contract test
suite. Everything in DD-001 is written against `read/cas`; nothing in DD-001
knows what git is. (DD-001, DD-002.)

**P5 No budget counter in v1.** Paid budget = sum of static per-lane envelopes
held by pinned nodes; model spend = zero by policy default; Drive bandwidth
uncapped in v1. A shared counter enters only with dynamic paid claiming, which
is a later ADR. G7 is reworded to "bounded by static envelopes". (KB-001 G7,
DD-001 scope.)

**P6 Planner is a port with two implementations.** `DeterministicPlanner`
(v1: policy-driven, no model) and `AgentPlanner` (later). Both emit the same
`EpochPlan` record; consensus consumes `EpochPlan` and nothing else. Negotiation
records are inputs to `AgentPlanner`, never to consensus. v1 roles are worker
and planner only; "verifier" is `sync reconcile` plus a fleet status report;
"scout" is deferred. RF9 and RN6 are satisfied structurally. (OQ-003, DD-003
scope.)

**P7 Completion records carry output descriptors.** A completion lists
`(relpath, size, md5, remote_id, contract fields)` for every file it wrote,
the same shape as a manifest entry (F4). A consumer pulls by descriptor and
verifies md5; no Drive listing is needed to find inputs, and S7 is checked
before download. (OQ-004, DD-001.)

**P8 Push has a write scope.** `build_plan` refuses any relpath outside the
prefixes the node owns in this epoch (its claimed units' partitions plus
node-scoped paths such as its status files). Ownership is enforced by the
planner, not by convention. Single-node mode has scope "everything", which is
today's behaviour byte for byte (G8). (OQ-004, storage engine.)

**P9 Policy lives on a protected ref in the ledger repository.** Nodes read
policy at epoch start; only the owner writes it. Policy is one versioned record
(`fleet policy v1`): node pins, envelopes, tier per task class, turn caps.
(OQ-005, DD-001 policy record.)

**P10 Version skew is a claim precondition.** A node advertisement carries
its contract versions (F10); a unit names the versions it requires; a claim
with a mismatch is refused by consensus before any work. (RN3, DD-001.)

**P11 Leases tolerate clock skew explicitly.** A lease carries the claimer's
declared expiry; readers apply a fixed tolerance; an adversarial row covers a
device with a clock hours off. (INV-004 seed.)

**P12 Node id = profile id + alias.** No new identity system (F7).

**P13 Scoring is the first fleet workload, nearly unchanged.** The unit is
`(schema@v, model, day)`; the output is the existing day partition; the
selector exists (F15). Two changes only: `state.csv` becomes node-local and is
never pushed (F16), and `score run` is admitted in the registry with
`paid=false, model=true` (F9). The consumer-side pull set is F17. (KB-001 G1,
INV-002 amendment, OQ-004.)

**P15 Capability advertisement is measured, not declared.** A node advertises
what a probe can verify: contract versions (F10), which Ollama models answer
(F18), whether the EODHD key and Drive token resolve (never their values),
and its last N online windows from its own journal. No free-text GPU class.
(RF1, DD-001 Node record.)

**P14 Data plane needs exactly three additions.** `download` on the backend
port, pull-by-descriptor in the engine, write scope in the planner. Everything
else (manifest, reconcile, idempotent update-by-id) stays. (OQ-004 → ADR-003.)

## 4. Goal amendments to put to the owner

| Goal | Proposed change | Why |
|---|---|---|
| G1 | Keep. Measure on the first real corpus re-score: wall time with 1, 2, 3 GPU nodes. | Already measurable once P13 lands. |
| G2 | Keep; add "enforced by pinning in v1, by claiming later". | P2. |
| G6 | v1 fleet view = ledger snapshot + each node's pushed `STATUS.md/json` (already produced daily). | Cheap and honest; no journal transport needed. |
| G7 | Reword to "bounded by static envelopes per node; a shared counter is a later ADR". | P5. |
| G10 | Keep; strengthen the measure: the simulation's daily cycle passes with `AgentPlanner` absent, not merely disabled. | Proves the layering. |
| new G11 (guess) | A node joins the fleet with one command that writes its advertisement; the fleet assigns claimable work to it in the next epoch without other nodes changing. | S6 as a goal. |

## 5. Owner questions, ranked by what they gate

Only four of the nine in SESSION-001 gate the trajectory; the rest can be
answered later without reopening a decision.

1. Devices, GPUs, online windows, which hold the EODHD key and a Drive token,
   which have a Claude or OpenAI subscription. Gates policy record, S2, ADR-004.
2. Is a private hosted git repository (GitHub, same account as this repo)
   acceptable as the ledger medium, with one deploy key or fine-grained token
   per device? Gates ADR-001 and the S4U credential spike.
3. Is failover of a pinned paid lane by owner action acceptable in v1? Gates
   P2 and removes S3/S4 for paid work from the first release.
4. Is "no agent turn in the daily steady state, agents only for exceptions"
   the intended v1, with `AgentPlanner` deferred until the deterministic fleet
   has run a week? Gates P6 and the order of DD-003.

Later, non-gating: approval channel for proposals (5), scoring currency and
daily model budget (6), untrusted devices (7), harness headless behaviour (8),
turn caps (9).

## 6. Trajectory (revised 2026-09-13 by D12)

| Phase | Artefacts and code | Exit criterion | Real effects |
|---|---|---|---|
| 0 | KB-001 v0.3 signed; KB-002 v0.2 (section 2 corrections, devices); GLOSSARY v0.3; OQ-001 to OQ-006 with recorded decisions. | Owner signs goals; P1 to P15 each accepted, amended or rejected. | none |
| 1a | Scoring stream, separate small change (D4): the runner keeps N requests in flight; `local` defined by node-declared endpoints, not the `ollama/` prefix (D3); quantisation in the model id. | Throughput measured in articles per hour at fixed concurrency on this box; all scoring tests pass. | GPU minutes on this box |
| 1b | S4U git spike (D11): an empty private repository, one deploy key on this box, a harmless logged-off task that fetches it. | Fetch succeeds from the S4U session, or the fallback (interactive-session task for the two daily records) is chosen. | new empty repository |
| 1c | DD-001 consensus: records Node, Unit, Slice, Claim, Lease, Completion, EpochPlan, Policy, Pass; Ledger port (P4); Planner port (P6). INV-004 seeds (section 7). ADR-001, ADR-004 PROPOSED. | An implementer can build phase 2 without asking a question. | none |
| 2 | `fleet/` package: FakeLedger, DeterministicPlanner, N-node in-process simulation over `LocalBackend`; property tests for paid-once, one-owner, lease expiry, tie-break, version skew, clock skew. | Every INV-004 CRITICAL/HIGH consensus row has a passing oracle over the fake. | none |
| 3 | DD-002 git ledger; GitLedger over a local bare repository passing the same contract suite; two-device race spike on the real remote. ADR-001 ACCEPTED. | Loser of a race within seconds observes the winner before acting, 100 of 100 trials. | hosted repository |
| 4 | Data plane (P14, D6): `download`, pull by descriptor, write scope; `state.csv` node-local (F16); ADR-003. Registry: admit `score run` and `score bench` with `paid=false, model=true`. Linux node: mirror seeded over the LAN, cron invoking the runner (D10). | Simulation runs S1 end to end with a fake scorer; the Linux node pulls yesterday's descriptors in a real epoch. | LAN copy of the data root |
| 5 | Rebench as the first fleet unit on the Linux box (D2): the decisive configuration plus 32 GB candidates, throughput axis, 14B control. Pass record written from the result and approved (D7). | A model is chosen by the fixed decision rule; the fleet lifecycle (claim, pull, score, push, completion) proved on a job of hours. | GPU hours on the Linux box; Drive writes under the scores prefix |
| 6 | Embeddings unit type and the one-year pass (D8, D9); Legion joins when docked (D5); quality gate every N days. | G1 measured with one, two and three nodes; zero duplicates on reconcile; embeddings current for the universe window. | GPU hours; Drive writes |
| 7 | Pinned paid lanes in policy; the daily job on this box becomes the pinned node for all lanes with fleet on. | G8 proven: same daily job, same outputs, one extra claim record per lane. | daily job config change |
| 8 | DD-003 negotiation; `AgentPlanner` and verifier behind the Planner port; harness on the subscription (this box) or a local model (Linux); turn caps; OQ-006 systemd adapter. | S8 and S9 pass in simulation with a scripted agent, then with one real harness. | subscription quota |

Phases 2 and 3 are where the quality bar is paid for; nothing after them may
change the Ledger port. Phases 1a, 1b and 1c are independent and can run in
parallel.

## 7. INV-004 seeds (adversarial scenarios to write before any ADR is accepted)

- FS-01 Split brain: two nodes each believe they hold the same claimable unit
  after a partition; only one completion may be accepted; the other's output
  is discarded deterministically by md5 mismatch with the accepted descriptor.
- FS-02 Stale lease: holder sleeps mid-unit; lease expires; re-claim; the
  original holder wakes and tries to complete. Its completion is refused
  (CAS on the claim it no longer holds).
- FS-03 Double claim within seconds on the real remote: loser sees conflict
  and does zero work.
- FS-04 Version skew: node with `scheduler-commands-v1` claims a unit
  requiring v2; refused before work.
- FS-05 Clock skew: a device 6 h behind renews a lease; readers must not
  expire it early or late beyond tolerance.
- FS-06 Pinned lane node absent for a week: fleet reports the lane behind, no
  other node fetches it (G2), owner is notified (RN5).
- FS-07 Policy ref edited by a non-owner node: refused by the medium (branch
  protection) and detected by the fleet (policy digest mismatch).
- FS-08 Ledger repository unreachable: node completes local work, pushes
  nothing, claims nothing new; daily single-node job unaffected (G8).
- FS-09 Drive folder-creation race between two nodes under write scope: both
  own disjoint prefixes so no shared folder is created concurrently; test the
  one shared ancestor.
- FS-10 Completion without data: a completion record whose descriptor md5 does
  not match the remote file; consumer refuses and reports.
- FS-11 Harness rate window exhausted mid-epoch (S8): planner degrades to
  deterministic; epoch completes; owner told.
- FS-12 Ledger growth: 90 epochs of claims and completions; compaction keeps
  the working snapshot under a fixed size and history reconstructable.

## 8. Revised agenda for the session (about 75 minutes)

| Slot | Topic | Output |
|---|---|---|
| 10 | Section 2 facts, especially F1 (scoring is already partitioned) and F16 (its state file is shared). | KB-002 v0.2. |
| 15 | Goals: section 4 amendments; sign section 3 and 3a. | KB-001 v0.3. |
| 15 | P1 to P5 (epoch, two regimes, no heartbeat, two-op port, no counter). | OQ-001/OQ-002 recommended options. |
| 10 | P6 (planner port, v1 without agents) and the four gating questions. | OQ-003/OQ-005 recommended options. |
| 10 | P7, P8, P14 (data plane). | OQ-004 recommended option. |
| 10 | Trajectory and INV-004 seeds; agree phase 1 scope. | INV-001 v0.3, this file v0.2. |
| 5 | Parking lot. | |

## 9. Parking lot (untriaged)

- Local bare git repository as the "real" ledger in CI; hosted remote only in
  the spike and production.
- Use the ledger repository's own `git log` as the fleet audit view before
  building any UI.
- `drive.file` visibility across devices sharing one OAuth client: verify
  before ADR-003.
- Whether scoring embeddings should become the first claimable unit instead
  of `event@5` scores (cheaper, GPU-only, no LLM).
- Node-scoped status files (`STATUS.md/json` per node) as the fleet view's
  raw material.

## 10. Design rounds of 2026-09-13 and the owner's decisions

Three rounds in conversation; this section is the record. The layering of
KB-001 3a was kept throughout: nothing below gives an agent ownership.

### 10.1 The fleet as agreed

| Node | Hardware | Standing role |
|---|---|---|
| Windows box (today's node) | small GPU; EODHD key; Drive token; daily job at 05:00 under S4U; Claude and OpenAI subscriptions | Pinned fetcher for every lane; owner's console; approval point; subscription harness host. Unchanged except one claim record per lane and one completion record per news day. |
| Linux box | RTX 5090, 32 GB, at home | Primary scorer; holds a full mirror of the data root; runs the batched local model endpoint. |
| Legion laptop | RTX 5090, 24 GB, travels | Opportunistic scorer; claims only when docked and on AC, one or two days per lease. |

No node is special in the protocol: the Windows box is pinned by policy, the
two scorers are interchangeable claimants.

### 10.2 The heavy task, sized

Two scoring streams, one unit type `(schema@v, model, day)`:

- Daily top-up: yesterday's partition, one to three thousand targeted
  articles, one card, about an hour. Does not need a fleet.
- Backfill: the universe subset back to 2021 is about 2.3 million articles;
  the last 365 days about 616 thousand. At the measured single-stream speed
  of a 14B model the full pass is roughly 2,000 GPU hours. The task is
  throughput-bound, not coordination-bound; the largest lever is batched
  serving (D3), the second is concurrency in the runner (D4).

### 10.3 One epoch, end to end

```text
05:00  Windows   refresh -> reindex -> push -> status --write
                 + claim(pinned lanes) + completion(news day D-1, descriptors)
hourly Linux     fleet tick: read ledger; if no EpochPlan for today, compute
                 it deterministically and CAS it (first node wins; the plan is
                 a pure function of ledger + policy + STATUS); claim K pending
                 days sized to measured throughput, lease = estimate x 1.5;
                 pull inputs by descriptor (articles for D, price sidecars);
                 score on the batched local endpoint; push partitions under
                 write scope; write completions; renew leases
docked Legion    same tick when docked and on AC; small leases; an expired
                 lease is re-claimed by the Linux box and resumed from the
                 chunks already in the partition
any    any node  score status / eval read the partitions; state.csv is
                 node-local and never pushed
```

Pending days are those with a news completion and no ok scoring completion
for the pass's `(schema, model)`; newest first for the top-up, then the
backfill window. Policy caps backfill days held per node per epoch.

### 10.4 What agents do (negotiation layer, all asynchronous through records)

1. Propose a pass: schema, model, quantisation, window, estimated GPU hours
   from measured throughput, inside the envelope. One `(schema, model,
   quantisation)` per pass, because mixed models across days break the panel
   comparison. Owner approves; only then does the planner emit those units.
2. Quality gate: every N scored days a verifier runs the existing eval and
   panel tooling and writes a critique record; a collapsing signal or rising
   invalid share pauses the pass and notifies the owner.
3. Triage: OOM, endpoint down, a day that keeps failing, a descriptor md5
   mismatch. Deterministic policy handles the common cases (reassign the
   day, mark the node degraded); the agent handles the rest.
4. Bench proposal: which configurations to run on the Linux box before a pass
   is committed.

Steady state has no agent turn (G10). The harness is Claude Code headless on
the Windows box under the subscription, or a local model on the Linux box,
with the same role prompt; agents read the ledger and write records only.

### 10.5 Decisions (owner, 2026-09-13)

| Id | Decision | Why | Artefact |
|---|---|---|---|
| D1 | Devices and standing roles as in 10.1. | Facts; VRAM and uptime decide where heavy work goes. | KB-002 section 4 |
| D2 | Rebench on the Linux box first; the bench is the first fleet unit. Add a throughput axis (articles per hour at the run's concurrency); keep the fixed decision rule (next-session edge, coverage at least 0.6, tie on speed, schema wins only if both finalists improve); keep the 14B as control. The median market proxy is accepted for ranking, not for absolute claims. | Proves the fleet lifecycle on a job of hours; the ranking is unbiased by a proxy that is the same for every candidate. | OQ-002, phase 5 |
| D3 | Batched local serving (vLLM or equivalent) on the Linux box, Ollama as fallback. Policy declares each node's local endpoints and model ids; "local" and zero cost follow from that, not from a model-id prefix. Quantisation is part of the model id. | Largest throughput lever; LiteLLM already talks to OpenAI-compatible endpoints. | OQ-005 |
| D4 | Scoring runner concurrency lands first as a separate scoring-stream change. | Pays off on one machine; sets the throughput number every lease is sized from. | phase 1a |
| D5 | The laptop claims only when docked and on AC, one or two days per lease; lid close mid-unit is the normal path. | Its value is extra throughput when present; the fleet never waits on it. | OQ-002 |
| D6 | The Linux box mirrors the full data root, seeded once over the LAN, kept current by pull by descriptor each epoch. Drive stays the plane of record. Its write scope is scores and embeddings only. | Per-epoch pulls become a few files; the node can run status, reindex and eval locally. | OQ-004 |
| D7 | Two-tier approval. Explicit, on the Windows box: start or change a pass, change policy, anything paid. Automatic from day one: every reversible action (reassign a day, pause on the quality gate, mark degraded, retry a pull). | Comparability across days is a one-way door worth one approval; everything else is cheap to undo. | OQ-003 |
| D8 | Backfill scope: the last year first, extended by envelope. | A quarter of the cost; enough to test the signal across regimes; the chosen model is seen at scale within days. | OQ-002 |
| D9 | Embeddings are a second unit type from the first real epoch, on the same day units. | Cheapest unit; exercises the fleet with no GPU risk; the "everything" tier the scoring design wanted. | OQ-002 |
| D10 | Linux scheduling: cron invoking the same registry command through the runner for the first release; a systemd adapter with three-plane observation later. | Not on the critical path; the runner already owns locks, journal, timeout and typed results. | OQ-006 |
| D11 | Ledger: a private GitHub repository under the owner's account, one deploy key per device, the policy ref read-only for nodes. First spike: a harmless logged-off task on the Windows box fetching the empty repository. Fallback if it fails: a small interactive-session task writes the daily job's two records. | Free, atomic on push, auditable, per-device credentials like the Drive token. | OQ-001, phase 1b |
| D12 | Order: runner concurrency, S4U git spike, rebench through the fleet, then embeddings and the one-year pass. | Each step de-risks the next and none waits on a decision. | section 6 |
| D13 | Steady state runs with no agent turn; agent duties are 10.4 only. | G10 and RN6 by construction. | OQ-003, OQ-005 |
