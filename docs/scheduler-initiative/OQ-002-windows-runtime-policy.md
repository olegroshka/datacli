---
id: OQ-002
title: What Windows account, dispatch, time and power defaults should schedules use?
status: STABLE
question_state: RESOLVED
owner: Oleg Roshka
last_reviewed: 2026-08-17
target_resolution: 2026-08-24
version: 1.0
sources:
  - KB-001
  - KB-002
  - ADR-002
  - DD-001
  - https://learn.microsoft.com/en-us/windows/win32/taskschd/principal-logontype
  - https://learn.microsoft.com/en-us/windows/win32/api/taskschd/ne-taskschd-task_instances_policy
  - https://learn.microsoft.com/en-us/windows/win32/api/taskschd/nf-taskschd-itrigger-get_startboundary
  - https://learn.microsoft.com/en-us/windows/win32/taskschd/monthlydowtrigger
  - https://learn.microsoft.com/en-us/windows/win32/taskschd/tasksettings
depends_on: [KB-001, KB-002, INV-004, ADR-002, DD-001, GLOSSARY]
referenced_by: [INV-001, INV-003]
---

# OQ-002 - What Windows runtime defaults should schedules use?

## Why this remains open

This is user policy, not merely an implementation detail. It also contains a
previous contract error: `IgnoreNew` was recommended while the product promised
a datacli `skipped_overlap` record. Windows cannot start the losing runner under
that policy, so no such record can exist. INV-004 requires that contradiction
to be resolved before approval.

Microsoft's logon contract also constrains the promise. `InteractiveToken`
runs only in an existing interactive session. `S4U` stores no password but has
no access to network or encrypted files, so it cannot be treated as unattended
Google Drive execution. Password logon requires native credential registration;
datacli must never receive or persist that password.

## Policy dimensions

1. Windows principal and whether the job may run while the user is logged out.
2. Start after a missed calendar trigger.
3. Wake the computer.
4. Start/continue on battery.
5. Gate on network availability in Windows or preflight inside the runner.
6. Same-job overlap and cross-job resource overlap.
7. Maximum runtime.
8. Automatic restart after failure.
9. Local wall-clock, timezone-change and DST semantics.
10. What backend dispatch history is observable when the runner never starts.

## Recommended first-release defaults

- Run with `InteractiveToken` as the current user and only while that user
  remains logged on. A locked session is acceptable; logout is not. `show` and
  creation output state this limitation prominently.
- `StartWhenAvailable = true` for calendar jobs. Describe it only as permission
  for a delayed start; do not promise replay of every missed occurrence or an
  exact nominal `scheduled_for` value.
- `WakeToRun = false`; expose an explicit `--wake` choice.
- AC power only for starting by default; expose an explicit battery override.
  Set `DisallowStartIfOnBatteries=true` but
  `StopIfGoingOnBatteries=false`: switching power source must not hard-stop an
  active mutation. A power-gated launch may produce backend-only evidence and
  no datacli run record.
- Do **not** use the Windows network gate in v1. Let the runner start, preflight
  connectivity/credentials and record an environment failure. There is no
  automatic retry; the user may run again. This favours observability over an
  opaque delayed/suppressed launch.
- Task Scheduler multiple-instance policy `Parallel` so every dispatch can
  reach the runner.
- Runner same-job and cross-job resource policy: acquire an OS-held lock with
  zero wait; journal the loser as `skipped_overlap` rather than queueing.
- Runner-owned soft timeout of 12 hours, overridable per job within a sane
  bound. Any Task Scheduler execution limit is a longer safety cap; if it hard
  terminates the process, recovery marks the run `abandoned` rather than
  claiming a controlled timeout.
- No automatic restart in v1. Current command exit codes do not distinguish a
  transient network failure from permanent configuration or quota failure.
- Calendar intent is Windows system-local wall-clock time. Generate an
  offset-free `StartBoundary`, record the Windows timezone at validation/install
  and flag a later zone change as drift. Microsoft documents that offset-free
  boundaries use the machine's timezone/DST information; a nonexistent spring
  time advances to the earliest existing time. Autumn repeated-time behaviour
  remains an integration spike and must be documented before STABLE.
- Backend history and datacli run history remain separate. If Task Scheduler
  history is unavailable, status reports `history unavailable/unknown`; it does
  not infer a launch from a nominal trigger.

## Alternative: run while logged out

This may be desirable for unattended overnight operation, but it is not a
small switch:

- `S4U` is unsuitable for Drive/network jobs and encrypted user files.
- `Password` logon stores credentials through Windows Task Scheduler and needs
  a native credential flow; the password must never appear in datacli argv,
  stdin handling, logs, definitions or temp files.
- a dedicated service account changes file/token ownership and configuration
  resolution.

Therefore true logged-out execution should be deferred from v1 unless the owner
makes it a requirement and approves a separate principal/credential ADR.

## Resolution criteria

- Google token and API credentials resolve under the selected principal.
- No password enters datacli arguments, logs, definitions or temporary files.
- Every overlap rejection that reaches a runner is observable; an OS-suppressed
  launch is shown only to the extent Windows provides evidence.
- Paid work is not automatically repeated without a clear failure taxonomy.
- Defaults are safe for a personal Windows laptop and explicit in `show`.
- DST, timezone change, sleep, logout, battery and disabled-history tests have
  recorded expected results on supported Windows versions.

## Human question

Is “same user, logged-on/locked sessions only, no wake, AC-only, start when
available, runner-level network preflight, no automatic retry” acceptable for
v1, or is true logged-out overnight execution a first-release requirement? If
logged-out execution is required, principal/credential design becomes a
separate approval gate rather than an adapter flag.

## Resolution output

Resolved by owner authorization on 2026-08-17: the recommended first-release
defaults are accepted as an amendment to ADR-002 and bind the Windows XML
fixtures.
