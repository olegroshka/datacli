---
id: ADR-002
title: Use Windows Task Scheduler through generated XML and schtasks.exe
status: STABLE
decision_state: ACCEPTED
owner: Oleg Roshka
last_reviewed: 2026-08-17
version: 1.2
sources:
  - KB-002
  - Microsoft schtasks documentation, accessed 2026-08-17
  - Microsoft Task Scheduler schema and settings documentation, accessed 2026-08-17
depends_on: [KB-002, ADR-001, DD-001, GLOSSARY]
referenced_by: [INV-001, INV-003, INV-004, OQ-002, ADR-005]
---

# ADR-002 - Use Windows Task Scheduler through generated XML and schtasks.exe

## Approval warning

Accepted by the owner through the scheduler implementation authorization on
2026-08-17, including OQ-002's recommended runtime defaults.

## Context

The first operating platform is Windows. The schedule must persist without a
datacli daemon and expose native next-run, last-run, enable/disable, run-now and
stop operations. Datacli also needs Task Scheduler settings beyond a minimal
daily trigger: missed-run handling, overlap policy, power/network controls,
working directory and a stable action.

## Proposed decision

Implement the first `SchedulerBackend` with Windows Task Scheduler 2.0.

- Generate a complete Task Scheduler XML definition from `JobSpec` plus the
  resolved runner action.
- Register/update through `schtasks.exe /Create /XML ... /F`.
- Query definition through `schtasks.exe /Query /XML`. Runtime state parsing
  must not depend on localised prose; a spike must prove the required fields are
  locale-stable or revise this boundary before the ADR can become stable.
- Use `schtasks.exe /Run`, `/End` and `/Delete` for lifecycle operations.
- Invoke all processes with argument arrays and `shell=False`.
- In v1, use a validated datacli-owned task-name prefix in an existing Task
  Scheduler folder. Do not assume `schtasks` creates a missing folder. A nested
  `\datacli\...` folder requires an explicit, tested folder-provisioning API and
  is an optional refinement.
- Install exactly one exec action: absolute Python interpreter, absolute runner
  path/module, profile ID, job ID, desired generation and definition digest,
  with an explicit working directory.
- Resolve the `InteractiveToken` principal from the effective Windows process
  token (`whoami.exe`), not mutable inherited username/domain environment
  variables.
- Embed the definition digest and datacli ownership metadata in the task
  description or another inspectable field so status can detect drift.
- Use Task Scheduler `Parallel` for multiple instances. The runner's OS-held
  per-job lock records and rejects the losing invocation as `skipped_overlap`.
  `IgnoreNew` is incompatible with a datacli run record because it suppresses
  runner startup entirely.
- Treat `/Run` success as a dispatch receipt only, never as job completion.
- Treat `/End` as a cancellation request until Windows process-tree integration
  tests prove that nested subprocesses are terminated.
- Create registration XML in a same-user restricted temporary file, validate
  encoding/escaping, and remove it after the registration attempt. It contains
  no secret-bearing fields.

Do not add `pywin32` or make PowerShell a runtime dependency in the first
implementation. XML provides the richer settings surface while `schtasks.exe`
is already present on supported Windows versions.

The precise principal, power, network and missed-run defaults are deliberately
left to OQ-002. Backend suppression can happen before the runner starts; the
adapter exposes that as a backend observation when Windows provides it and
never fabricates a datacli `RunRecord`. Task Scheduler history/event logging may
be unavailable, so status must preserve `unknown`.

## Alternatives considered

### A. PowerShell ScheduledTasks cmdlets

Viable, but rejected in the proposal as the primary boundary because Python
would need to generate PowerShell, handle another quoting layer and serialise
CIM output. It remains a diagnostic fallback.

### B. Task Scheduler COM through pywin32

Viable and strongly typed at runtime, but adds a Windows-only binary dependency
and COM-specific test complexity. Reconsider only if XML/schtasks cannot expose
required reliable state.

### C. `schtasks /Create` flags only

Rejected because the flag surface is less expressive and makes advanced
settings harder to review and test than a versioned XML template.

### D. APScheduler daemon

Rejected as the Windows timing engine for the reasons in ADR-001. It does not
remove the need to arrange process startup, recovery and account context.

## Consequences

### Positive

- Native persistence, triggers and management without a datacli service.
- Generated XML is inspectable, fixture-testable and exportable.
- No new Windows-specific Python dependency.
- Task state remains visible in the standard Windows UI.

### Costs and risks

- Task XML schema and Windows result codes need careful fixtures.
- Locale and privilege differences must be tested.
- `schtasks` may be insufficient for locale-neutral runtime observations; the
  proposal must be revisited rather than papering over that gap with prose
  parsing.
- XML registration may expose account-policy complexity to the user.
- Absolute repository/interpreter paths can drift; status and doctor must flag
  them rather than silently recreating tasks.
- The OS can suppress launch before datacli journals anything, so backend and
  execution histories remain distinct.

## Acceptance criteria

- XML round-trips through Task Scheduler on supported Windows.
- `schedule status` combines backend state with datacli run state.
- Exported XML contains no secrets and exactly one runner action.
- The action's generation/digest is checked by the runner, and a stale action
  cannot mutate domain data.
- Two simultaneous dispatches start two runner processes but yield one active
  execution and one journalled `skipped_overlap` result.
- Fake command fixtures test registration without running paid/network work.
- The adapter satisfies DD-001 without adding Windows fields to common records.
- Clean-user/non-admin, non-English locale, disabled-history and nested-child
  stop cases have explicit integration results, including AC-to-battery and OS
  execution-limit behaviour.

## Decision outcome

Accepted on 2026-08-17. OQ-002 is resolved to its recommended first-release
defaults.

ADR-005 records the required implementation revision: definition queries stay
on `schtasks /XML`, while a fixed read-only PowerShell/ScheduledTasks JSON probe
provides locale-neutral runtime observations. Task mutations remain exclusively
XML plus argument-array `schtasks.exe` calls.

The authorised 2026-08-17 integration fixture round-tripped one real root task
through register, observe, run and remove under the current logged-on user. Its
single action produced a correlated successful datacli journal record.
