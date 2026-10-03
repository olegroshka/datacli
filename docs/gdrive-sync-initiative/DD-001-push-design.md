---
id: DD-001
title: Push design: one listing per unit, bounded concurrency, visible progress, request deadlines
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-10-03
version: 0.1
sources:
  - KB-001, storage/cli.py, storage/gdrive.py, storage/engine.py
depends_on: [KB-001]
referenced_by: [BOOTSTRAP-GDS, INV-001, OQ-001]
---

# Push design

Four changes, each independently shippable and tested, in the order below.
None changes the `sync` argv contract or its resource claims, the manifest
format (version 1) or the Drive folder layout.

## 1. One listing per unit (replaces the per-file duplicate guard)

Today a file the manifest does not know costs a `files.list` lookup before
its create (KB-001 section 1). Instead, when the plan contains any create
candidate (a file without `remote_id`), the push calls `list_remote` once
for the unit, builds `{relpath: newest copy}` from `remote_tree`, and
resolves ids from it: a file present remotely becomes an update, an absent
one a create with no lookup. The listing already exists for the manifest
rebuild and costs a few pages for the whole tree.

The invariant of the 2026-09-11 incident holds in a stronger form: the
reconciliation happens against the complete tree rather than one folder at
a time, and a create is issued only for a path the listing did not contain.
A create that races a concurrent writer is excluded by the lock contract
(the push holds the unit's root exclusively while it runs).

Folder resolution keeps its cache; new directories still cost a lookup and
a create each, once.

## 2. Bounded concurrency for uploads

A pool of `workers` threads (default 4, `[sync] push_workers` in
`datacli.toml`, 1 restores today's behaviour) takes plan items from a
queue. Each worker owns its own authenticated `httplib2.Http` and Drive
service (the library objects are not thread safe, KB-001 section 5),
built from the same credentials. The main thread records completions in
the manifest in **completion order**, saves it on a cadence (every 20
completed files or 5 seconds, and at the end) instead of after every file,
and prints progress. A crash loses at most one cadence of manifest writes;
the files are on Drive and the next push's reconciliation (section 1) finds
them by listing instead of re-uploading. Per-file retries stay inside the
worker; a 429 on any worker pauses the whole pool for the backoff so the
quota is not fought over.

Large files gain nothing from concurrency (they are bandwidth bound) and
are uploaded by the pool like any other item; the order of the plan puts
the big files first so the small ones overlap with their tail.

Expected effect on the 2026-10-03 FINRA push: 2,958 files at about 2 s each
serially, 1 h 50 min; with one listing and four workers, about 1.3 s per
file divided by four, under 20 minutes.

## 3. Progress the operator can see

- A progress line to **stderr** every 30 seconds while uploads are in
  flight: `push eodhd: 57/102 files, 1.1 GB of 1.9 GB, 118 MB/min, about 7
  min left`, and one line per unit at its end. Stderr is captured by the
  runner like stdout, so it appears in `run.log` once the step ends and in
  the terminal immediately when run by hand.
- The same figures are written to `<root>/.sync/push_progress.json` (unit,
  files done and total, bytes done and total, started, updated, rate)
  every cadence, atomically, and removed at the end of the unit. This is
  the file a human or `schedule doctor` reads during a long push; the
  manifest's `uploaded_at` stops being the only evidence. The file is never
  read by the push itself.
- `sync status` prints the progress file when one exists ("push in
  progress since 12:39, 2,764/2,958").

Streaming a step's output inside the runner is the scheduler initiative's
concern (OQ-001 Q3); this design does not depend on it.

## 4. A wall-clock deadline per request

Every Drive call runs with a deadline (`[sync] request_deadline`, default
600 s, at least the socket timeout times the client library's retries). A
call that overruns is abandoned, classified as transient, and retried by
the push loop's own attempts; after the last attempt the file is reported
failed and the push continues with the next file (`--keep-going`, the
default). The deadline is enforced by running the call in the worker and
waiting with a timeout on the main thread's future; an abandoned worker
thread is left to die with the process. The per-chunk socket timeout and
the library's retries stay as they are.

The unit's own deadline is the runner's execution timeout; the push does not
add one.

## 5. Acceptance

- Unit tests on `LocalBackend` and a fake Drive service that counts
  requests: a first push of N new files in M directories issues exactly one
  listing, M folder lookups and creates, and N creates with no per-file
  lookup; a repeat push issues nothing; a file whose `remote_id` was deleted
  remotely falls back to the listing and never creates a second copy.
- Concurrency: with 4 workers and a fake backend that sleeps, N small files
  finish in about N/4 of the serial time; the manifest after a simulated
  crash mid-batch contains only confirmed files, and the next push
  re-uploads nothing that was confirmed.
- Progress: the progress file exists during the push with monotone
  counters and is gone after it; the stderr line appears at the cadence;
  `sync status` shows an in-progress push.
- Deadline: a fake call that never returns is abandoned at the deadline,
  the file is reported failed, the push finishes and returns 1.
- Live: one push of a temporary root with 500 small files and one 300 MB
  file under the real backend, before and after, timed; no duplicate on
  Drive afterwards (`sync reconcile` reports none).

## 6. Adversarial scenarios

| id | scenario | wrong outcome | oracle |
|---|---|---|---|
| S1 | manifest lost, files on Drive | a second copy of every file | listing-based reconciliation; test with a fake tree |
| S2 | crash after a create, before the manifest save | a second copy on the next push | the next push lists first and finds the file by path |
| S3 | two workers create the same new directory | two folders of one name | folder resolution serialised under one lock, cache shared |
| S4 | 429 on one worker while others continue | the whole pool throttled into failures | a shared pause; test that no worker issues a request during it |
| S5 | a stalled request on one worker | the push waits forever | the deadline abandons it; test with a never-returning fake |
| S6 | the token expires mid-push | every worker fails at once | credentials shared, refreshed once under a lock; test with a fake expiry |
| S7 | a file changes while being uploaded | the manifest records a stat that does not match the bytes sent | stat taken before the upload, as today; the next push sees the newer mtime and re-uploads |
| S8 | the progress file survives a crash | `sync status` reports a push that is not running | the file carries the process id; stale ones are reported as stale |
