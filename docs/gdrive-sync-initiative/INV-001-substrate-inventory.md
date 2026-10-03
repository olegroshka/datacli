---
id: INV-001
title: Google Drive sync initiative inventory and work packages
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-10-03
version: 0.1
depends_on: [KB-001, DD-001, OQ-001]
referenced_by: [BOOTSTRAP-GDS]
---

# Inventory

## Register

| Id | File | Status | Version | Role |
|---|---|---|---|---|
| BOOTSTRAP-GDS | `README.md` | DRAFT | 0.1 | entry point, problem, rules |
| KB-001 | `KB-001-current-push-facts.md` | DRAFT | 0.1 | what a push does and what it cost on 2026-10-03 |
| DD-001 | `DD-001-push-design.md` | DRAFT | 0.1 | the four changes, acceptance, scenarios S1 to S8 |
| OQ-001 | `OQ-001-owner-questions.md` | OPEN | 0.1 | concurrency and quota, the FINRA backup, runner streaming |
| INV-001 | `INV-001-substrate-inventory.md` | DRAFT | 0.1 | this register |

## Work packages

"Needs owner" marks a real external effect; everything else is local.

| WP | scope | design | depends on | needs owner | effort |
|---|---|---|---|---|---|
| WP1 | one listing per unit; `_find_files` retired from the upload path; fake Drive service with request counting | DD-001 section 1 | nothing | no | half a session |
| WP2 | progress line, progress file, `sync status` reads it | DD-001 section 3 | nothing | no | half a session |
| WP3 | worker pool, per-worker service, manifest cadence, shared 429 pause | DD-001 section 2 | WP1 | OQ-001 Q1 (default workers) | one session |
| WP4 | request deadline | DD-001 section 4 | WP3 | no | half a session |
| WP5 | live timing on a temporary root, before and after; KB-001 gains the numbers | DD-001 section 5 | WP1 to WP4 | yes: one real push of a throwaway folder | an hour |
| WP6 | the eodhd job: nothing changes in its definition; record the first scheduled push's duration | | WP5 | no | a look at `schedule history` |

Order: WP1 and WP2 first (each alone already helps), then WP3, WP4, WP5.

## Priority queue

1. WP1 and WP2.
2. OQ-001 Q1 and Q2 answered by the owner; WP3, WP4.
3. WP5, then close the initiative with the measured numbers in KB-001.

## Out of scope

- Streaming a step's output in the scheduler runner (scheduler initiative).
- Changing what is backed up (which roots, caches) beyond OQ-001 Q2.
- A different backend or a different manifest format.
