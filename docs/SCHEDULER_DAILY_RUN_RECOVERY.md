# Scheduler daily run: review, fixes landed, and recovery

Handoff for a fresh session. Second pass, written 2026-09-11 evening after the
first pass of the same day was re-verified against the working tree, git, the
scheduler run records under `%LOCALAPPDATA%\datacli`, the Windows System log
(power, logon, NTFS events), the task XML and the sync manifest. Everything in
"Verified state" was checked, not inferred. Re-verify anything older than a day
with the commands in section 7 before acting on it.

This supersedes the first pass of this file and
`docs/SCHEDULER_REAL_RUN_HANDOFF.md` (2026-08-19). Two findings of the first
pass were wrong and are corrected in section 1.

## 0. Copy/paste prompt for the new session

```text
You are continuing the datacli scheduler initiative in
C:\Users\olegr\PycharmProjects\datacli. Read docs/SCHEDULER_DAILY_RUN_RECOVERY.md
first. Sections 4 to 6a are done; the job is generation 4, runs while
logged off (S4U), and the 2026-09-12 run is evaluated in section 6a. Inspect
the next 05:00 run against gates A to D the same way. Also read AGENTS.md
and docs/scheduler-initiative/README.md for the substrate rules.

Goal: the daily job eodhd-all-sync-20260820-r3 must refresh every EODHD lane,
reindex, and push the delta to Google Drive every day without human help, and
Google Drive must hold exactly one current copy of every file.

Use .venv\Scripts\python.exe. Never print tokens, secrets, or API keys. Never
run a paid refresh, a real Drive upload, a Drive delete, or a Windows task
change to test code: use fakes, tmp roots, and dry-runs. Real effects need my
explicit OK in the session, one gate at a time (section 6).

Never stage or commit .codex/, .claude/, AGENTS.md, or datacli.toml.
```

## 1. What the second pass changed in the diagnosis

1. **The 09-09 "16 h refresh" was a 10.4 h nap.** The run log has one
   37,512 s gap (09:42:11 to 20:07:23 local) and the System log shows
   `Kernel-Power 42` (entering sleep) at 09:42:10 and `Power-Troubleshooter 1`
   (returned from low power) at 20:07:04. Awake work was about 5.8 h, inside
   the normal 5.5 to 6.5 h band. There is no slow lane to hunt.
2. **The 12 h timeout did not "fail"; two clocks disagreed.** The runner
   measured elapsed time with `time.monotonic()`, which on Windows keeps
   counting through sleep, so it saw 16.2 h and voided the run at the step 2
   boundary. `subprocess.communicate(timeout=...)` waits on an OS relative
   timeout that excludes sleep, so the child was never killed. Confirmed on
   this machine: since the 09-10 boot the awake clock is about 11 h behind
   the monotonic clock, exactly the sleep the log shows.
3. **09-10 had no run because nobody was logged on.** Winlogon events: log-off
   at 01:59:42, Windows Update reboots at 02:00 and 02:01, next log-on at
   08:32:36. The task's principal is `InteractiveToken` ("run only when user
   is logged on"), so the 05:00 trigger could not start it, and
   `StartWhenAvailable` does not replay a condition miss. This is the accepted
   OQ-002 limitation showing up in production.
4. **`ac_only` is moot.** The machine is a desktop (`PCSystemType=1`, no
   `Win32_Battery`).
5. **The manifest loss still has no proven mechanism, but a strong suspect.**
   Event 6008 records an unexpected shutdown on 2026-08-31 at 10:49:06 local,
   the same morning as the last good push (push finished 11:02:18 local by
   the runner's clock; the two timestamps disagree by 13 minutes, which is
   within the heartbeat slop of event 6008). No NTFS repair ran at the 09-08
   boot (`Ntfs 98` "healthy", no chkdsk), no `.tmp` was left in `.sync`, no
   scheduled step touched the manifest between 08-31 and 09-11 (09-08 and
   09-09 never reached the push step), and the Codex working-tree edits date
   from 08-19. `save_manifest` used rename without fsync, and `load_manifest`
   turned any unreadable file into "empty" silently, so whatever the
   corruption was, it was invisible and then overwritten by the 09-11 push.
   Open question 8.1 stands: ask the user whether anything was run by hand.

## 2. Verified state (three planes, kept separate)

### Desired state (datacli job store)

| Item | Value |
|---|---|
| Profile | `a17568ea-c0a4-4f6f-9031-d4b3bab3216a` |
| Active job | `eodhd-all-sync-20260820-r3`, generation 4 (since 2026-09-11 20:26 UTC), enabled |
| Tombstoned jobs | `eodhd-all-sync-20260819`, `eodhd-all-sync-20260820-r2` (IDs not reusable) |
| Trigger | daily 05:00 system local, `ac_only=true`, `wake_to_run=true`, `start_when_available=true`, `logon=logged_off` (S4U) |
| Steps | 1 `eodhd refresh --with-fundamentals --run`, 2 `eodhd reindex`, 3 `sync push --run` |
| Policy | fail-fast (`on_step_failure=stop`), `retry=none`, timeout 43,200 s, same-job overlap skip |
| Digest | `96577315…1d2c45`, matches the installed Windows task (generation 3 was `87b4241c…ddec0c`) |

Generation 4 was committed through `schedule edit … --daily 05:00 --wake
--logged-off` from an elevated terminal on 2026-09-11; steps and policy are
unchanged, only the logon mode and the config-file binding moved. Note the
trap that surfaced on the way: `datacli.toml` is fingerprinted into the
runtime bindings, so any edit to it makes `schedule status` report
`incompatible` until the job is re-committed with `schedule edit <job>`.

### Backend observation (Windows Task Scheduler)

Task `\Datacli-a17568ea-c0a-eodhd-all-sync-20260820-r3`: enabled, Ready,
last run 2026-09-11 05:00:01, last result 1 ("datacli workflow step failed"),
next run 2026-09-12 05:00. Principal `S4U` (runs whether or not the user is
logged on; no password stored; `InteractiveToken` until generation 3),
`DisallowStartIfOnBatteries=true`, `ExecutionTimeLimit=PT13H`,
`MultipleInstancesPolicy=Parallel`, `StartWhenAvailable=true`,
`WakeToRun=true`. Task Scheduler history logging was **enabled** on
2026-09-11 (`Microsoft-Windows-TaskScheduler/Operational`), so the next
silent day has a Windows record.

### Execution history (datacli run records, job r3)

| Date (UTC start) | Outcome | Refresh | Reindex | Push | Note |
|---|---|---|---|---|---|
| 08-20 09:40 | abandoned | failed | not run | not run | first dispatch, effect unknown |
| 08-20 09:43 | succeeded | ok | ok | ok | full initial push, 3,111 files, 8.7 GB |
| 08-22, 08-23 | succeeded | ok | ok | ok | |
| 08-24, 08-25 | failed | **failed** | not run | not run | `RemoteDisconnected` in `fetch_eodhd_uk_eu_etf_splits.py:153` |
| 08-26 to 08-31 | succeeded | ok | ok | ok | 08-31: 68 files, 1.4 GB, 3,067 unchanged; last good push |
| 09-01 to 09-07 | no run | | | | machine off after an unexpected shutdown on 08-31 |
| 09-08 14:26 | failed | **failed** | not run | not run | `ConnectionResetError(10054)` in the same fetcher, same line |
| 09-09 04:00 | timed_out | ok (5.8 h awake, 16.2 h calendar) | not run | not run | machine slept 09:42 to 20:07 local |
| 09-10 | no run | | | | user logged off 01:59, reboot 02:00, logged on 08:32; InteractiveToken task skipped |
| 09-11 04:00 | failed | ok (5.6 h) | ok | **failed** | manifest read as empty; 3,156 planned as `new`; 74 uploaded as duplicates, then `The read operation timed out` |

Run IDs and log paths: `datacli.py schedule --json history eodhd-all-sync-20260820-r3`.

### Local data (`python -m eodhd.cli status --no-discovery --no-color`)

20 fresh, 2 stale, 0 absent, about 71.15 M rows across 22 datasets. The local
refresh is healthy. Note: `datacli.py eodhd status` from a non-interactive
shell prints `headless usage: …` and exits 255; use `python -m eodhd.cli
status` in scripts.

### Sync manifest and Google Drive

- `C:\Users\olegr\PycharmProjects\btest\data\raw\eodhd\.sync\gdrive.json`
  holds exactly the 74 entries uploaded on 09-11 (all with today's
  `uploaded_at`, all with new Drive ids). The `.sync` folder itself dates
  from 2026-08-02 and survived; only the file's content was lost.
- Drive folder `1YGuE21YP0k89vhVWtMWx-nMM-Zi-AbwA` (`datacli/eodhd`) holds
  the Aug-31 copies of everything plus a second copy of those 74 files
  (STATUS.json/.md, `_datacli_index.parquet`, `_datacli_meta.json`, the five
  `index_ref` files, and `news/articles/2019-01-01` to `2019-03-06`). The
  old/new id pairs for three of them are in the first-pass table; the full
  list is the `remote_id` column of today's manifest versus the reconcile
  output (section 5).

### Repository

- `main` after this pass: the two Codex fixes landed as
  `904a5410` (`fix(eodhd): import _atomic …`) and `07348058`
  (`fix(scheduler): serialise dataclass collections …`), followed by the
  feature commits of section 4. Push to `origin/main` is part of the
  handoff checklist in section 6.
- Untracked and never committed: `.codex/`, `AGENTS.md`,
  `docs/SCHEDULER_REAL_RUN_HANDOFF.md`, `docs/SCORING_SESSION_KICKOFF.md`.
- Pre-existing lint debt unrelated to this work: `isort --check-only` fails
  on about 30 committed `eodhd/` files (the `import _atomic` block) and
  `black --check` on `eodhd/build_news_symbol_daily.py`,
  `eodhd/fetch_eodhd_news.py`, `tests/test_panel_eval.py`,
  `tests/test_scoring_bench.py`. Every file touched by this pass is clean.

## 3. Root causes, with evidence

### RC1. Sync manifest lost silently; "new" meant create, not update

- `storage/engine.py` `load_manifest` returned an empty manifest for an
  unparseable file without a word, and `save_manifest` renamed without
  fsync. An unclean power-off (event 6008, 2026-08-31 10:49 local) is the
  prime suspect for the loss.
- `storage/gdrive.py` `upload` called `files().create` whenever the manifest
  had no `remote_id`; Drive allows duplicate names in a folder.
- Today's plan listed all 3,156 files as `new`; 74 were created before the
  first network error.

### RC2. Push aborted on first failure and never retried

- `storage/cli.py`: per-file `try/except`, `break` unless `--keep-going`; no
  retry, no backoff, no HTTP timeout on the Drive client, no `num_retries`.

### RC3. Seven fetchers had no HTTP retry

`session.get` was unguarded in `fetch_eodhd_uk_eu_etf_dividends.py`,
`fetch_eodhd_uk_eu_etf_prices.py`, `fetch_eodhd_uk_eu_etf_splits.py`,
`fetch_eodhd_uk_eu_index_ref_prices.py`, `fetch_eodhd_us_etf_dividends.py`,
`fetch_eodhd_us_etf_prices.py`, `fetch_eodhd_us_etf_splits.py`. The three
universe fetchers go through `_api_get`, which already catches
`RequestException` and retries a 429 once. Three of the last twelve refreshes
died in `fetch_eodhd_uk_eu_etf_splits.py:153` on a transient TCP reset.

### RC4. Daily job ran uncommitted code; origin/main was broken

Commit `0363f0c6` added `_atomic.` calls to 29 writers but the import to only
15. The working-tree fix (dated 08-19) is what kept the job alive; it is now
committed as `904a5410`.

### RC5. Sleep, logon, and silence

- Sleep mid-run plus two disagreeing clocks (section 1, items 1 and 2).
- `InteractiveToken` principal plus an unattended reboot (section 1, item 3).
- Nothing notified anyone; Task Scheduler history is off.

## 4. What was implemented (all committed, all tested with fakes)

### 4.1 Working-tree review landed (RC4)

Duplicate `import _atomic` removed from `status_eodhd.py`, the misplaced
import in `fetch_eodhd_bulk.py` moved to its block, the `SCENARIOS.md` typo
reverted, line endings normalised. Two commits, see section 2.

### 4.2 Drive push is idempotent and resilient (RC1, RC2)

- `storage/engine.py`: `ManifestError` for an unreadable manifest (never
  "empty"); `quarantine_manifest` keeps it as `gdrive.json.corrupt-<stamp>`;
  `save_manifest` fsyncs before the atomic replace; `rebuild_manifest`
  reconstructs a manifest from a backend listing (md5 match → recorded as
  pushed; content differs → recorded with the remote id so the next push
  *updates*; remote-only → orphan entry) and reports duplicate copies;
  `build_plan` orders uploads metadata first, then newest-modified first.
- `storage/backends.py`: optional `list_remote`, `trash`, `retryable` on the
  port; the local backend implements all three (it is the test double).
- `storage/gdrive.py`: `upload` without a `remote_id` looks the file up by
  name in its folder and updates it (reconcile-before-create); every request
  runs with `num_retries=5` over `httplib2.Http(timeout=120)`; `list_remote`
  pages the whole app-visible tree and maps it to relpaths under
  `remote_root` (pure `remote_tree` helper); `trash` soft-deletes.
- `storage/cli.py`: `execute_push` quarantines an unreadable manifest and
  exits 2; with `--run` and an empty manifest it authenticates, rebuilds the
  manifest from the backend and only then plans; per-file uploads retry
  transient errors (5 attempts, 2 s doubling to 60 s); the push continues
  past a file that still fails (**default changed**; `--fail-fast` restores
  stop-at-first; `--keep-going` is accepted and now redundant); new
  `sync reconcile [--run] [--trash-duplicates] [--report PATH]` command.
  The scheduled step 3 needs no change: the code default covers it.
- `scheduler/commands.py`: `--fail-fast` admitted for scheduled `sync push`.
- Tests: `tests/test_storage_push.py` (unreadable manifest, rebuild with zero
  creates, first push, md5/newest duplicate choice, retry with backoff,
  non-transient not retried, keep-going default and fail-fast, plan order,
  reconcile dry-run/run/backup/trash), `tests/test_storage_gdrive.py` (fake
  Drive service: lookup-before-create, paging, trash, retryable
  classification, `num_retries`), `tests/test_storage_sync.py` updated.

### 4.3 One HTTP retry helper for the fetchers (RC3)

- `eodhd/_http.py` `get_with_retry`: retries `ConnectionError`, `Timeout`,
  `ChunkedEncodingError` and HTTP 429/500/502/503/504 (429 honours
  `Retry-After`), 4 attempts, 2 s doubling to 30 s, logs each retry with the
  ticker; other 4xx returned untouched; exhausted connection errors
  re-raise.
- The seven fetchers call it inside `try/except requests.RequestException`
  and, when retries are exhausted, skip the ticker exactly the way their
  non-200 branch does (same counters, same `time.sleep(DELAY)` or not).
- Tests: `tests/test_eodhd_http.py`.

### 4.4 Awake-clock timeout, keep-awake, per-step timing, notifications (RC5)

- `scheduler/power.py`: `keep_system_awake()` holds
  `SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)` for the run;
  `awake_clock()` is `QueryUnbiasedInterruptTime` on Windows (sleep
  excluded), `time.monotonic()` elsewhere.
- `scheduler/runner.py`: the timeout budget is measured on `awake_clock`; the
  power request is journaled as `power_request {system_required: bool}`;
  after every terminal outcome `scheduler/notify.py` writes
  `runs/<job>/LAST_RUN.txt` (and `LAST_FAILURE.txt` when not
  succeeded/no_op) and runs `[scheduler] notify_command` if configured
  (failures only unless `notify_on = "always"`); the notifier result rides
  inside the `run_terminal` payload so the journal still ends with
  `run_terminal`.
- `scripts/notify_toast.ps1` (toast, interactive sessions only) and
  `scripts/notify_eventlog.ps1` (Application event log, works from S4U) for
  `notify_command`. Config snippets are in each script's header.
- `eodhd/cli.py`: the refresh prints `-> ok in h:mm:ss` per step and a
  "Refresh timing" table at the end, so a slow day is attributable without
  reading the log.
- Tests: `tests/test_scheduler_runtime.py` (6 h awake inside a 16 h calendar
  run succeeds; 13 h awake times out; power request released on exceptions;
  status files; notify command list form; TOML settings).

### 4.5 Substrate sync

INV-004 gained AS-30 (sleep mid-run), AS-31 (logged-out trigger), AS-32
(silent state-file loss inside a step) and AS-33 (no notification); DD-001
now defines the timeout on the awake clock and adds the notify step to the
runner ordering contract; INV-002 records the new `sync push` semantics and
`sync reconcile` (FORBID for scheduling); OQ-002 has an "Observed
consequences" section; INV-001 versions bumped. The job contract
(steps, digest) did not change.

## 5. Recovery of the Drive tree and manifest

Every step here is a real Drive effect and needs the user's OK in the session.
Use `$env:DATACLI_NONINTERACTIVE = "1"` so nothing opens a browser.

1. **Inventory** (read-only, needs the cached token):
   `.venv\Scripts\python.exe datacli.py sync reconcile --report tmp\reconcile.json`
   Expect about 3,100 remote paths, 74 of them with two candidates, the
   summary line `N match local, M differ, …`. The report JSON lists every
   duplicate with the id to keep and the ids not chosen.
2. **Dedupe rule** (already implemented in `engine.choose_remote`): keep the
   copy whose md5 matches the local file; if none matches, keep the newest.
   Review the duplicate list. For the 74, the Aug-31 copy and the 09-11 copy
   have identical content only for files that did not change since 08-31;
   `STATUS.json` for example changed, so the new copy wins there.
3. **Rebuild the manifest and trash the losers**:
   `.venv\Scripts\python.exe datacli.py sync reconcile --run --trash-duplicates`
   The previous manifest is kept as `.sync\gdrive.json.bak-<stamp>`; losers go
   to Drive trash, not permanent deletion.
4. **Dry-run** `datacli.py sync push` (no `--run`) and confirm the plan is
   `changed` entries only, in the low hundreds, with zero `new`.
5. Then the real push, under gate C in section 6.

If the user prefers not to trash anything yet, step 3 without
`--trash-duplicates` still rebuilds the manifest; the daily push will then
update the chosen copies and the losers stay as stale extras until trashed.

**Done on 2026-09-11 evening (no Drive effect):** step 1 ran read-only and
step 3 ran *without* `--trash-duplicates`. Inventory (`tmp\reconcile.json`):
3,145 remote paths, 3,077 match local by md5, 68 differ (the daily delta),
0 remote-only, 11 local-only, 74 paths with two copies; for all 74 the 09-11
copy is the md5 match and is the keeper. The manifest was rebuilt to 3,145
entries and the previous 74-entry file kept as
`.sync\gdrive.json.bak-20260911T182241Z` (copy it back over `gdrive.json`
to revert). The dry-run push now plans 79 uploads (68 changed, addressed by
remote id, plus 11 new) and cannot create duplicates.
**Done 2026-09-11 20:18 UTC with the user's OK:** `sync reconcile --run
--trash-duplicates` moved the 74 Aug-31 copies to Drive trash; a following
read-only reconcile reports 0 duplicate paths and the dry-run push plans
79 uploads. Gate C is the next real push.

## 6. Real-run gates and owner decisions

Gates, one at a time, each with the user's explicit OK:

- **Gate A, refresh**: let the 05:00 run fire (or `schedule run … --wait`
  only when the user says so). Accept when step 1 is `succeeded`, the
  "Refresh timing" table shows every lane inside the 08-22 to 08-31 band, and
  `python -m eodhd.cli status` shows no new stale lanes. Check the journal
  has `power_request {"system_required": true}`.
- **Gate B, reindex**: step 2 `succeeded`, `_datacli_index.parquet` newer than
  the run start.
- **Gate C, push**: step 3 `succeeded`, summary `0 failed`, a following
  dry-run says "Everything in sync", and a Drive search for `STATUS.json`
  under folder `1YGuE21YP0k89vhVWtMWx-nMM-Zi-AbwA` returns exactly one file.
- **Gate D, three-plane report**: desired (generation, digest), backend
  (exists, enabled, last result, next run), execution (run id, per-step
  outcomes, timestamps), data (status counts), sync (uploaded count and
  bytes, single-copy check), plus `LAST_RUN.txt`. Never collapse these into
  "in sync".

Owner decisions, resolved on 2026-09-11 evening:

1. **Logged-off execution: done.** A harmless S4U probe task proved that a
   logged-off task on this machine loads the user profile, sees the EODHD
   key, reads the Drive token and reaches both APIs (OQ-002 amendment).
   `TriggerSpec.logon = logged_off` was added (`--logged-off`), and the job
   was re-committed as generation 4 with `LogonType=S4U`. Registering an
   S4U task needs an elevated terminal; datacli never handles a password.
2. **Task Scheduler history: enabled** (elevated `wevtutil`).
3. **Notification: configured.** `[scheduler]` in `datacli.toml` runs
   `scripts/notify_eventlog.ps1` on every outcome (`notify_on = "always"`);
   it writes to the Application event log, source `datacli`, event 1000 for
   success and 1001 for anything else. A toast cannot reach the desktop from
   a session-0 task, which is why the event log was chosen. To reach a phone,
   wire a webhook or SMTP script into the same `notify_command`.
4. **Pushed to origin.**

If a job definition change is ever needed, use the managed flow
(`schedule edit` or draft + `enable`), never a raw `schtasks` change, and
verify with `schedule --json doctor`.

## 6a. First run under the new code: 2026-09-12 05:00 (evaluated)

Run `20260912T040001.739801Z-96e97d4d6f01`, generation 4, S4U principal.

| Plane | Observation |
|---|---|
| Backend | Task Scheduler history: launched 05:00:01 on the calendar trigger, action started, completed 10:54:56. Machine woke at 04:59:33; zero sleep events during the run. |
| Principal | Six python processes in session 0 (logged-off session) for the whole run; nobody had to be logged on. |
| Runner | `power_request {system_required: true}`; step 1 `succeeded` in 5:27:47 (24 lane steps ok, timing table in the log, no lane above 1:00:22); step 2 `succeeded`, 172,838 index entries; step 3 `failed`. |
| Push | 84 planned (78 files, 258 MB uploaded; 3 touch); **6 failed** after four retries each with `RedirectMissingLocation`. All six are the parquet files above the 100 MB resumable chunk (`news/news_symbol_daily`, five `prices_daily`). |
| Notification | `LAST_RUN.txt` and `LAST_FAILURE.txt` written; Application event 1001 (Error) from source `datacli` at 10:54:56 with the one-line summary. |
| Drive | After the failure: 3,157 paths, 0 duplicates, 6 differ. No duplicate was created by the failed resumable uploads (they address the existing id). |

Gate A **pass**, gate B **pass**, gate C **failed then recovered**, gate D this table.

Root cause of the 6 failures: the explicit-timeout `httplib2.Http` added on
2026-09-11 left `follow_redirects=True`. A resumable upload answers every
chunk with `308 Resume Incomplete` and no `Location` header, which httplib2
treats as a broken redirect; the stock `googleapiclient.http.build_http`
excludes 308 for exactly this reason. Fixed in `fd8f7853` (redirects off on
the Drive client, regression test in `tests/test_storage_gdrive.py`). A manual
`sync push --run` with the fix then uploaded the 6 files (1.2 GB) with 0
failures; the dry-run reports "Everything in sync" and a read-only reconcile
shows 3,157 remote paths, all matching local by md5, 0 duplicates.

Remaining watch items for 2026-09-13: the scheduled push must complete with
`0 failed` on its own (the fix is in the tree the job runs), and the event
log should show 1000 rather than 1001. The us_common fast-catch-up backlog
(94 price pairs, 14 dividend pairs more than 7 days behind) is unchanged by
any of this and is a separate `refresh us_common --run` decision.

## 7. Commands

```powershell
# Read-only state, run any time
.venv\Scripts\python.exe datacli.py schedule --json list
.venv\Scripts\python.exe datacli.py schedule --json status eodhd-all-sync-20260820-r3
.venv\Scripts\python.exe datacli.py schedule --json history eodhd-all-sync-20260820-r3
.venv\Scripts\python.exe datacli.py schedule logs eodhd-all-sync-20260820-r3
.venv\Scripts\python.exe datacli.py schedule --json doctor
.venv\Scripts\python.exe -m eodhd.cli status --no-discovery --no-color
schtasks /query /tn "\Datacli-a17568ea-c0a-eodhd-all-sync-20260820-r3" /v /fo LIST
type "%LOCALAPPDATA%\datacli\profiles\a17568ea-c0a4-4f6f-9031-d4b3bab3216a\runs\eodhd-all-sync-20260820-r3\LAST_RUN.txt"

# Drive (uses the cached token; must not open a browser)
$env:DATACLI_NONINTERACTIVE = "1"
.venv\Scripts\python.exe datacli.py sync status                 # offline plan + manifest state
.venv\Scripts\python.exe datacli.py sync reconcile --report tmp\reconcile.json   # read-only listing
.venv\Scripts\python.exe datacli.py sync push                   # no --run: plan only

# Power/logon evidence (PowerShell)
Get-WinEvent -FilterHashtable @{LogName='System'; Id=@(1,41,42,107,6005,6006,6008)} -MaxEvents 40 | Sort-Object TimeCreated | Format-Table TimeCreated, Id, ProviderName
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Winlogon'} -MaxEvents 20 | Format-Table TimeCreated, Id

# Quality gates before every commit (touched files only; see section 2 for pre-existing debt)
.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider --basetemp tmp\pytest-full
.venv\Scripts\python.exe -m black --check <changed files>
.venv\Scripts\python.exe -m isort --check-only <changed files>
.venv\Scripts\python.exe -m mypy scheduler
git diff --check
```

## 8. Open questions

1. What emptied `.sync\gdrive.json` between 08-31 and 09-11? Ask the user
   whether anything was run by hand; otherwise the 08-31 unexpected shutdown
   is the working assumption, and the fsync plus loud-failure changes make a
   repeat both less likely and visible.
2. Why does event 6008 place the 08-31 shutdown at 10:49 local while the
   runner journaled the push end at 11:02 local? Probably heartbeat slop in
   6008; not worth more time unless the manifest loss recurs.
3. Should the timeout policy stay at 12 h awake time now that sleep is
   excluded? The 08-22 to 08-31 band is 5.5 to 6.5 h, so 12 h remains a
   generous cap.

## 9. Fixed facts and paths

| Item | Value |
|---|---|
| Repo | `C:\Users\olegr\PycharmProjects\datacli` |
| Python | `.venv\Scripts\python.exe` (3.13) |
| Config | `datacli.toml` (`[eodhd] data_root`, `[sync] backend, remote_root, gdrive_client_secrets`, `[scheduler] notify_on, notify_command`); editing it requires `schedule edit <job>` to re-bind |
| Data root | `C:\Users\olegr\PycharmProjects\btest\data\raw\eodhd` |
| Sync manifest | `<data root>\.sync\gdrive.json` (74 entries today; about 3,145 on 08-31) |
| Drive token | `%USERPROFILE%\.datacli\tokens\gdrive.json` (never read or print) |
| Drive target | `gdrive:/datacli/eodhd`, folder id `1YGuE21YP0k89vhVWtMWx-nMM-Zi-AbwA` |
| Scheduler state | `%LOCALAPPDATA%\datacli\profiles\a17568ea-c0a4-4f6f-9031-d4b3bab3216a\` |
| Windows task | `\Datacli-a17568ea-c0a-eodhd-all-sync-20260820-r3` (S4U since generation 4, PT13H limit) |
| Last good push | run `20260831T040001.781131Z-0dc0f496034d`, 68 files, 1.4 GB |
| Sleep-voided run | `20260909T040001.835237Z-54c1e7812ae0` |
| Duplicate-creating run | `20260911T040001.997013Z-39e832991c25` |
