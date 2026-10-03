---
id: KB-001
title: How a push behaves today, measured on the 2026-10-03 run
status: DRAFT
owner: Oleg Roshka
last_reviewed: 2026-10-03
version: 0.1
sources:
  - storage/cli.py (execute_push, push_units), storage/gdrive.py, storage/engine.py
  - scheduler/runner.py (step output capture)
  - run 20261003T040001.986142Z-5819ec867206 of job eodhd-all-sync-20260820-r3; the .sync/gdrive.json manifests of the eodhd, finra and macro roots
depends_on: []
referenced_by: [BOOTSTRAP-GDS, DD-001, INV-001]
---

# Current push facts

## 1. What `sync push --run` does

`push_units` walks every configured sync unit in turn (today: the eodhd,
finra and macro roots, each with its own `.sync/gdrive.json` manifest and
Drive folder). For each unit `execute_push`:

1. loads the manifest; an empty manifest triggers `list_remote` (the whole
   app-visible Drive tree, 1,000 entries a page) and a rebuild, so a lost
   manifest never re-creates files (the 2026-09-11 incident);
2. plans by comparing every local file's size and `mtime_ns` with the
   manifest (`engine.plan`); payload cache dirs are skipped unless
   `--with-caches`;
3. uploads the planned files **one at a time**, in plan order, and saves the
   manifest (tmp file, `fsync`, atomic replace) **after every file**;
4. applies metadata-only touches, saves the manifest, prints
   `Push finished: N uploaded (X), F failed, R not run.`

Per uploaded file, `GDriveBackend.upload` issues, serially:

| step | requests | when |
|---|---|---|
| `_ensure_folder` | 0 when the directory is cached; one `files.list` plus one `files.create` per new directory | first file of each directory |
| `files.update` with the manifest's `remote_id` | the resumable upload: one initiation request plus one request per 100 MB chunk | file known to the manifest |
| `_find_files` duplicate guard, one `files.list` | one | file unknown to the manifest (first push, or a 404 on update) |
| `files.create` | the resumable upload, as above | file unknown and absent remotely |

So a small new file costs three round trips (lookup, initiation, chunk) and
one `fsync`; a small known file two.

Resilience: `httplib2.Http(timeout=120)` per socket operation,
`num_retries=5` inside the client library (5xx, 429, socket errors, with
randomised exponential backoff), and the push loop's own five attempts per
file with backoff 2 to 60 seconds for the errors `retryable` classifies.
Nothing bounds a request's total wall-clock time beyond those layers.

## 2. Measured on 2026-10-03

The 05:00 job ran the old per-ticker refresh (generation 4; generation 5
with `--fast` was enabled at 07:50, after the run had started). Step 1 took
7 h 24 min, step 2 three seconds, step 3 (`sync push --run`) started at
12:24:03 local:

| unit | manifest before | planned | result | wall time | rate |
|---|---|---|---|---|---|
| eodhd | 3,098 files | 102 changed files, 1.87 GB (prices, fundamentals, news, state files) | 102 uploaded | 12:24:08 to 12:38:59, 15 min | 130 MB a minute; about 9 s per file |
| macro | 3 | 3 files, 1 MB | 3 uploaded | 4 s | |
| finra | **empty** (first push of this root) | 2,958 files, 1.3 GB (daily partitions of short volume, weekly flow, short interest, fails to deliver, 0.3 MB each) | 2,764 uploaded by 14:22 | 12:39:24 onward, about 1 h 50 min projected | 27 to 29 files a minute, about 2.1 s per file, 11 MB a minute |

Earlier pushes of the eodhd root alone: 69 to 75 files, 1.4 to 1.5 GB, 10
to 13 minutes (2026-08-23, 09-30, 10-01). The first full push on 2026-08-20
took 2 h 46 min. The job's execution timeout is 43,200 s.

Rates are set by request latency, not bandwidth: the same connection moved
130 MB a minute on big files and 11 MB a minute on small ones.

## 3. What the operator could see

- `scheduler/runner.py` runs a step with `subprocess.run`, captures stdout
  and stderr, and writes them to `run.log` **after the step returns**
  (`runner.py:349`). During step 3 the log ends with step 2's output;
  `schedule logs` and `schedule history` show the step as started and
  nothing else.
- The push itself prints one line per file (`[i/N] relpath (size) ...`)
  and no rate or estimate.
- The only live evidence of progress is the manifest's `uploaded_at`
  timestamps, written after every file. Reading the eodhd manifest alone
  (untouched since 12:39 while the finra unit was being pushed) looked like
  a hang; the finra manifest showed 29 files a minute. A push over several
  units cannot be judged from one manifest.
- The push worker kept an idle keep-alive socket to Google in
  `ESTABLISHED` and another in `CLOSE_WAIT` between requests; an idle
  socket is not evidence of a stalled request.

## 4. Locks and blast radius

The runner holds the job's resource claims for the whole run: the eodhd
root exclusively (step 1 mutates it), the finra and macro roots for the
push. Every other scheduled job with an overlapping claim is skipped
(`resource_overlap='skip'`) and every interactive mutation that claims the
eodhd root, such as `positioning build`, is refused until the run ends. On
2026-10-03 that blocked the positioning work for the whole push.

## 5. Library facts

`googleapiclient` resumable uploads use `DEFAULT_CHUNK_SIZE = 100 MiB`;
`HttpRequest.execute` loops `next_chunk` until the body returns, retrying
each chunk `num_retries` times. `httplib2.Http` objects are not safe to share
across threads; one per worker is the documented pattern. Drive's per-user
quota is on requests per 100 seconds, not bytes; the client library's 429
handling backs off but a sustained overrun degrades every worker at once.
