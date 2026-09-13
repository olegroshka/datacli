---
id: OQ-006
title: How does a Linux node run its fleet tick with the same three planes?
status: OPEN
owner: Oleg Roshka
last_reviewed: 2026-09-13
version: 0.1
sources:
  - KB-001 G6, G8, RF8
  - docs/scheduler-initiative/DD-001 (SchedulerBackend port), ADR-002 (Windows adapter)
  - Owner decision D10, 2026-09-13 (SESSION-001-PREP 10.5)
depends_on: [KB-001, KB-002, GLOSSARY, SESSION-001-PREP]
referenced_by: [INV-001]
---

# OQ-006 - Linux node scheduling

## The question

The scheduler substrate has one backend adapter, Windows Task Scheduler
(`scheduler/backends/windows.py`), with desired, backend and execution planes
kept separate. The Linux box (D1) needs a periodic fleet tick and a daily
mirror pull. What launches the runner there, and how honest is the backend
plane in the first release?

## Options

| Option | Sketch | Pull | Push |
|---|---|---|---|
| A. cron invoking the runner | A crontab line runs the same registry command through `JobRunner`; desired state and execution history are exactly today's; the backend plane is "unobserved". | Nothing new to build; locks, journal, timeout and typed results are all runner-owned. | No backend observation: `schedule status` reports the OS plane as unknown on Linux. |
| B. systemd timer adapter | A `SchedulerBackend` implementation over `systemctl` and unit files, with install, query, run_now, observe_history. | Full three-plane honesty; user-level timers survive logout. | Real work: unit-file generation, locale-neutral observation, the equivalent of ADR-005 for `systemctl show`. |
| C. Long-running fleet daemon | A datacli process loops and ticks. | Simplest to write. | Contradicts G4's "no always-on coordinator" in spirit and the substrate's no-daemon stance (ADR-001). |

## Owner decision in principle (2026-09-13, D10)

Option A for the first release, Option B afterwards. Conditions: the cron
line runs the same admitted registry command as the Windows job, never a
bypass; `schedule status` on Linux must say the backend plane is unobserved,
not infer it; the runner's awake-clock and keep-awake paths degrade explicitly
where the Windows power API is absent.

## What we need to learn

- Whether `scheduler/power.py` and `scheduler/locks.py` behave correctly on
  Linux (flock path exists; `QueryUnbiasedInterruptTime` fallback to
  monotonic).
- Whether the state root and profile identity resolve sensibly outside
  `%LOCALAPPDATA%`.
- What a user-level systemd timer needs from the port (Option B), recorded as
  the adapter contract before it is built.

## Target resolution

Option A verified on the Linux box in phase 4 (SESSION-001-PREP section 6);
Option B contract drafted in phase 8; ADR when B is built.
