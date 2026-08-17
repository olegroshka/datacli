---
id: KB-002
title: Current datacli execution model and scheduler constraints
status: STABLE
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - ../../datacli.py
  - ../../eodhd/cli.py
  - ../../macro/cli.py
  - ../../storage/cli.py
  - ../../storage/gdrive.py
  - ../../scoring/cli.py
  - ../../pyproject.toml
  - ../../scheduler/
depends_on: [KB-001, GLOSSARY]
referenced_by: [INV-001, INV-002, INV-003, INV-004, DD-001, ADR-001, ADR-002, ADR-003, ADR-004, ADR-005, OQ-001, OQ-002]
---

# Current datacli execution model and scheduler constraints

## Verified facts

1. The scheduler implementation lives in `scheduler/`: it includes the command
   registry, records/store, machine-wide locks, run journal/runner, management
   service/CLI and Windows backend.
2. `datacli.py` remains an interactive shell by default and now also delegates
   one-shot `datacli.py schedule ...` invocations without entering `cmdloop()`.
3. The underlying EODHD, macro, storage and scoring CLIs accept `argv` and
   return process-style integer exit codes. They are the usable headless
   surfaces today.
4. EODHD refresh builds an ordered plan and executes its steps sequentially as
   subprocesses. It stops at the first failure unless `--keep-going` is used.
5. EODHD refresh is a dry-run without its own `--run` flag. Routine refresh is
   documented as `refresh --fast --run`; reindex is a separate command.
6. Macro fetch is a dry-run without its own `--run` flag and depends on FRED
   and/or EODHD credentials in the user environment or existing key source.
7. Sync is a one-way push. It uses the configured `gdrive` or `local` backend,
   is a dry-run without its own `--run`, and checkpoints its manifest after
   each successful upload.
8. Google Drive push now derives authentication interactivity from the explicit
   execution context. Scheduled mode uses cached credentials with
   `interactive=False` and never opens browser consent.
9. The interactive shell's session log records command text, not stdout,
   stderr, duration, structured step outcomes or reliable exit state. It is not
   sufficient as scheduled-run history.
10. The project remains configured with `package = false` and has no installed
    console-script entry point. Windows tasks therefore use an absolute
    interpreter, `-m scheduler.runner`, explicit working directory and immutable
    profile/job/generation/digest arguments; status detects path drift.
11. Local configuration is repository-specific (`datacli.toml`), while Google
    OAuth credentials default to the current user's home directory. A task run
    as another Windows principal may resolve different configuration or lack
    access to credentials.
12. Rich output may assume an interactive terminal. Scheduled execution needs
    a no-colour, non-interactive output mode suitable for log files.
13. Direct EODHD refresh/reindex, macro fetch, sync push and config writes now
    acquire the same canonical same-user machine-wide locks as the runner.
14. Runtime data identity is environment-sensitive. `EODHD_DATA_ROOT` overrides
    repository config, macro data may use a separate configured root, and sync
    resolves its source/backend when the command starts.
15. Sync scans the EODHD data root. The default macro root is a sibling of that
    root, not its child, so `macro fetch -> sync push` does not currently back
    up macro data.
16. Legacy CLIs still expose integer process results, while the canonical
    registry returns typed outcomes/effects/retry guidance. EODHD refresh and
    sync push summaries now report completed, failed and unattempted work
    separately; runner status never parses that prose.
17. EODHD refresh launches nested child processes. Runner-owned soft timeout is
    backed by a Windows Job Object and its nested-child fixture passes. Task
    Scheduler `/End` descendant termination remains unproved and `stop` reports
    confirmation as unknown.
18. `schtasks /Query /XML` exposes the installed definition but not the required
    locale-neutral runtime fields. ADR-005 adds a fixed read-only
    PowerShell/ScheduledTasks JSON observation; failure remains unknown.
19. Windows registration resolves the effective process-token identity through
    locale-neutral `whoami.exe`; inherited `USERDOMAIN`/`USERNAME` are not
    authoritative because sandbox/run-as sessions can make them disagree.
20. The authorised harmless logged-on-user lifecycle passed on 2026-08-17:
    register, clean never-run observation, `/Run`, correlated successful
    journal record, result-0 observation, tombstone, delete and clean absence.
    Task Scheduler history was disabled and remained explicitly unavailable.
    The host registry reported `Windows 10 Pro`, display version `25H2`, build
    `26200.9168`, AMD64; this evidence is not generalized to other releases.

## Existing execution surfaces

| Family | Headless entry | Characteristics |
|---|---|---|
| EODHD | `.venv/Scripts/python.exe eodhd/cli.py ...` | broad command set; refresh delegates to subprocesses |
| Macro | `.venv/Scripts/python.exe -m macro.cli ...` | fetch/status/list; environment credentials |
| Sync | `.venv/Scripts/python.exe -m storage.cli ...` | status/push/login; Drive auth needs scheduled-mode hardening |
| Scoring | `.venv/Scripts/python.exe -m scoring.cli ...` | model/budget-dependent and potentially long-running |
| Interactive shell | `.venv/Scripts/python.exe datacli.py` | enters `cmdloop()`; unsuitable as a task action |

The paths above describe the current repository; they are not the future public
contract. ADR-001 proposes the durable runner boundary.

## Constraints induced by current behaviour

- A scheduled job must not drive `cmdloop()` or simulate terminal input.
- A single runner should invoke the existing headless surfaces until their
  domain operations are cleanly extracted behind shared command adapters.
- The runner and backend pass argument arrays with `shell=False`.
- `--run` belongs to the scheduled command after the command separator; the
  schedule-management CLI must not reuse `--run` for installation.
- Google authentication has an explicit non-interactive path and readiness
  checks explain how to run `sync login` manually.
- Job status must combine OS task state with datacli's own run journal; neither
  source is complete by itself.
- Locks span different jobs and direct interactive/headless mutations sharing a
  canonical data root, target, manifest, credential cache or config resource.
- The task action should not depend on ambient `PATH` or the current working
  directory.
- Runtime preflight must compare resolved data/config/backend bindings with the
  validated definition rather than trusting the environment inherited at
  execution time.
- Adapters must return typed outcomes such as success/no-op/failure; scheduled
  status must not parse human completion summaries.

## Current oracles

- CLI parser/help execution verifies that an entry point is callable.
- Unit/contract tests cover the scheduler substrate and fake Windows adapter.
  The harmless real Task Scheduler lifecycle passes; Google API/credential work
  and disruptive Windows environment matrices remain explicit-effect gates.
- Exit codes are the authoritative current machine signal. Human-oriented
  completion summaries must not be parsed as status.
- The source files listed in frontmatter are the oracle for this artefact. Any
  material change to those execution paths makes KB-002 and its downstream
  dependants candidates for `STALE`.
