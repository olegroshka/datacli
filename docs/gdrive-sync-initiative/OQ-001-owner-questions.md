---
id: OQ-001
title: Owner questions: concurrency and quota, the FINRA backup, runner streaming
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-10-03
version: 0.1
depends_on: [KB-001, DD-001]
referenced_by: [BOOTSTRAP-GDS, INV-001]
---

# Open questions

## Q1. How many workers, and who watches the quota?

DD-001 section 2 proposes four workers by default. Drive's per-user request
quota is per 100 seconds; four workers at about one request a second each
sit well inside it, but the number was not measured. Options:

- **A (recommended)**: default 4, `[sync] push_workers` in `datacli.toml`,
  the shared 429 pause as the safety valve, and WP5's live timing as the
  measurement before the default ships in a job generation.
- B: default 1 (today's behaviour) and opt in per machine.

## Q2. Is the FINRA root worth backing up at 3,000 files?

The 2026-10-03 push was slow because the FINRA root (daily partitions, 0.3
MB each) was being pushed for the first time. It is re-fetched from
FINRA's public files in minutes. Options:

- **A (recommended)**: keep it; after WP1 to WP4 the daily increment is a
  handful of files and the first push is a one-off that is now done.
- B: drop the finra unit from the push and rely on the public source.
- C: pack the daily partitions into monthly files before the push (a
  storage change outside this initiative).

## Q3. Should the runner stream a step's output?

Today `run.log` receives a step's output when the step ends
(`scheduler/runner.py:349`), so a two-hour step is silent. DD-001 section 3
works around it with a progress file and stderr lines, which the runner
still shows only at the end. Streaming belongs to the scheduler initiative
(its runner contract, DD-001 there). Recorded here so that it is not
forgotten; no option is proposed in this substrate.
