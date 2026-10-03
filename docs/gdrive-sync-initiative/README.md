---
id: BOOTSTRAP-GDS
title: Google Drive sync initiative, substrate entry point
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-10-03
version: 0.1
sources:
  - Shared Substrate conventions, as practised in docs/scheduler-initiative
depends_on: [KB-001, INV-001]
referenced_by: []
---

# Google Drive sync: make a push fast, visible and bounded

A small initiative. `sync push --run` backs the data roots up to Google
Drive once a day as the last step of the 05:00 EODHD job. On 2026-10-03 the
step took two hours for 3,000 small FINRA partition files while the runner
showed nothing, and the operator could not tell a slow push from a hung one
(KB-001). The code is correct and resumable; it is serial, chatty and
silent. This substrate fixes the intent and the acceptance before any code
changes.

## The problem in one paragraph

Every uploaded file costs three to four serial Drive round trips (folder
resolution, a duplicate-guard lookup, the resumable upload's initiation and
chunk) plus a manifest save with `fsync`, about two seconds per file on this
connection. Large files move at 130 MB a minute, so a root of a few big
parquet files pushes in minutes; a root of thousands of daily partitions
pushes in hours. The scheduler runner captures a step's output and writes it
when the step ends, so during those hours `schedule logs` shows the previous
step. The push holds the job's resource locks for the whole run, which
blocks every other job and every interactive mutation that reads the eodhd
root (the positioning builds on 2026-10-03 waited two hours).

## Where it stands (2026-10-03)

| Layer | Artefact | Status |
|---|---|---|
| Facts measured on the live push | KB-001 | DRAFT |
| Design: one listing per unit, concurrency, progress, deadlines | DD-001 | DRAFT |
| Register, work packages, scenarios | INV-001 | DRAFT |
| Owner questions: concurrency and quota, the FINRA backup | OQ-001 | OPEN |

## Standing rules

- The duplicate-tree incident of 2026-09-11 (`storage/gdrive.py` header) is
  the invariant: no change may create a second copy of a file on Drive. The
  manifest stays the source of truth for `remote_id`, and every create is
  preceded by a reconciliation against the backend's own listing.
- A push stays resumable: the manifest records a file as pushed only after
  the backend confirmed it, and never records a file it did not push.
- `sync` is a scheduled command (`scheduler-commands-v1`); its argv contract
  and resource claims do not change in this initiative. Streaming step
  output inside the runner belongs to the scheduler initiative and is only
  referenced here (OQ-001 Q3).
- Tests on the local backend and a fake Drive service first; one live push
  of a temporary root before the change ships in a job generation.
- Same edit protocol as the other substrates: one source of truth per fact,
  cite by id, bump `version` and `last_reviewed`, update INV-001 in the
  same change.
