---
id: OQ-003
title: Which commands and workflow-management syntax belong in the first release?
status: STABLE
question_state: RESOLVED
owner: Oleg Roshka
last_reviewed: 2026-08-17
target_resolution: 2026-08-24
version: 1.1
sources:
  - KB-001
  - INV-002
  - ADR-003
  - DD-001
depends_on: [KB-001, INV-002, INV-004, ADR-003, DD-001, GLOSSARY]
referenced_by: [INV-001, INV-003]
---

# OQ-003 - Which commands and workflow UX belong in the first release?

## Why this remains open

The architectural command boundary is proposed in ADR-003, but the smallest
clear user surface still needs a human choice. A one-line multi-step grammar is
compact but quote-heavy. A create/add-step sequence is explicit and easier to
inspect, at the cost of more commands.

## Recommended admitted commands

### Core

- `eodhd refresh ... --run`
- `eodhd reindex`
- `sync push ... --run`

### Optional but low-risk

- `eodhd status`
- `eodhd qc`
- `macro fetch ... --run`
- `macro status`
- `sync status`

### Deferred or forbidden

Keep the `DEFER` and `FORBID` classifications from INV-002 for the first
release, particularly scoring, probe, login, configuration and arbitrary shell
execution.

## Recommended syntax

### One-step convenience

```text
schedule add morning-refresh --daily 06:00 -- eodhd refresh --fast --run
schedule add drive-backup --daily 07:00 -- sync push --run
```

`schedule add` is already an explicit reversible mutation, so it should install
without an outer `--run`; this avoids collision with the scheduled command's
own execution flag.

### Multi-step workflow

```text
schedule create morning --daily 06:00
schedule step add morning -- eodhd refresh --fast --run
schedule step add morning -- eodhd reindex
schedule step add morning -- sync push --run
schedule show morning
schedule enable morning
```

`create` creates a non-executable `JobDraft`, not an empty `JobSpec` and not a
Windows task. Each `step add` validates its command statically. The first
`enable` finalises the complete draft, performs non-interactive readiness checks
without paid work or required live connectivity, commits one desired generation
and reconciles it to Windows. A draft can be listed and discarded. Later
presets may expand visibly into these ordinary steps:

```text
schedule add morning --daily 06:00 --preset refresh-reindex-push
```

### Management surface

```text
schedule commands
schedule list
schedule drafts
schedule show <job>
schedule status [job]
schedule history <job>
schedule logs <job>
schedule test <job>
schedule run <job> [--wait]
schedule pause|resume <job>
schedule stop <job>
schedule edit <job> ...
schedule delete <job>
schedule reconcile <job>
schedule discard <draft>
schedule purge <job> ...
schedule doctor [job]
```

The verbs have deliberately distinct semantics:

- `test` runs in the foreground through the shared runner and returns that exact
  `RunRecord`.
- `run` asks Windows to dispatch the installed action. Success means the request
  was accepted, not that a run started or completed. `--wait` correlates a new
  record only when unambiguous and otherwise returns timeout/unknown.
- `pause`/`resume` affect future dispatches and do not stop an active process.
- `stop` requests cancellation and reports whether the runner/process tree is
  confirmed terminal or remains unknown.
- `edit` commits a new desired generation, then reconciles the OS task; an
  installation failure remains visible instead of pretending atomic rollback.
  Multi-step edits use a draft based on an expected generation and apply once;
  they never expose half an edit to the runner.
- `delete` refuses by default while active, writes a tombstone and removes the
  OS task while retaining history/snapshots.
- `reconcile` explicitly repairs desired/backend drift. `status` and `doctor`
  remain read-only.
- `purge` is the separate destructive history/snapshot operation and must show
  what reconstructability will be lost.

## Alternative syntax

- Repeated quoted `--step "..."` options on one line: compact but creates a
  second quoting language inside cmd2/PowerShell.
- A literal separator such as `::` between steps: parseable but unfamiliar.
- Named routines only: easy but hides exact commands and reduces composability.
- Separate independent schedules: simple but cannot guarantee ordering.

## Resolution criteria

- A new user can create refresh-only and refresh/reindex/push schedules without
  understanding Windows Task Scheduler.
- The exact canonical steps are always inspectable.
- The parser does not need shell-string re-parsing.
- An incomplete workflow cannot accidentally run.
- A stale edit draft cannot overwrite a newer desired generation.
- A management command never reports dispatch acceptance as execution success.
- Active edit/delete/pause/stop races have explicit, testable outcomes.
- Existing datacli command flags remain unchanged after `--`.
- Tab completion can expose families, verbs and management operations.

## Human questions

1. Is the explicit `create -> step add -> enable` sequence clear enough for
   multi-step workflows?
2. Should macro fetch be admitted in v1 or wait until the daily EODHD/sync path
   is proven?
3. Should read-only status/QC jobs be admitted before there is a notification or
   report-delivery mechanism?

## Resolution output

Resolved by owner authorization on 2026-08-17: adopt the recommended core and
optional command set, explicit `create -> step add -> enable` workflow, and the
listed management surface. This is accepted through ADR-003.

## Implementation evidence

Every operation now renders contextual `--help` with behavior, safety
boundaries, defaults and examples. `schedule commands` includes canonical usage
and effect summaries for every admitted family/verb. The cmd2 completer shares
the parser operation/option and registry capability metadata, and adds
read-only completion for profiles, jobs, tombstones, drafts, step indexes,
paths, trigger values and allowlisted command arguments. Parity tests fail if a
parser operation/option or registry capability is omitted from completion.
