"""Keep the machine awake during a run, and measure time the way the OS does.

Background (run ``20260909T040001.835237Z-54c1e7812ae0``): Task Scheduler woke
the machine at 05:00, the refresh started, the machine went back to sleep at
09:42 and only resumed at 20:07. The child process finished normally after
5.8 h of awake work, but the runner measured 16.2 h on ``time.monotonic()``
(which keeps counting through sleep on Windows) and voided the run as
``timed_out`` before step 2. ``subprocess.communicate(timeout=...)`` on the
other hand waits on an OS relative timeout that excludes sleep, so the child
was never interrupted. Two clocks, two answers.

This module gives the runner one answer:

* :func:`keep_system_awake` holds an ``ES_SYSTEM_REQUIRED`` power request for
  the duration of the run, so an idle timer cannot put the machine to sleep
  mid-workflow (a lid close or a manual sleep still can).
* :func:`awake_clock` returns seconds that exclude sleep/hibernation on
  Windows (``QueryUnbiasedInterruptTime``) and falls back to
  ``time.monotonic()`` elsewhere. The execution timeout is measured on it, so
  the runner and the child-process wait agree.

Both are best-effort and never raise: a platform without the API simply gets
no power request and the monotonic clock.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import time
from typing import Callable, Iterator

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


def _set_execution_state(flags: int) -> int | None:
    """Call ``SetThreadExecutionState``; return the previous flags, or None."""
    if os.name != "nt":  # pragma: no cover - exercised on Windows only
        return None
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.SetThreadExecutionState.restype = ctypes.c_uint
        previous = kernel32.SetThreadExecutionState(ctypes.c_uint(flags))
    except Exception:  # noqa: BLE001 - best effort
        return None
    return int(previous) or None


@contextlib.contextmanager
def keep_system_awake(
    *, setter: Callable[[int], int | None] = _set_execution_state
) -> Iterator[bool]:
    """Hold a system-required power request while the block runs.

    Yields whether the request was actually granted (False off Windows or
    when the API call failed), so the caller can journal it.
    """
    held = setter(ES_CONTINUOUS | ES_SYSTEM_REQUIRED) is not None
    try:
        yield held
    finally:
        if held:
            setter(ES_CONTINUOUS)


def _unbiased_seconds() -> float | None:
    if os.name != "nt":  # pragma: no cover - exercised on Windows only
        return None
    try:
        value = ctypes.c_ulonglong()
        ok = ctypes.windll.kernel32.QueryUnbiasedInterruptTime(  # type: ignore[attr-defined]
            ctypes.byref(value)
        )
    except Exception:  # noqa: BLE001 - best effort
        return None
    if not ok:
        return None
    return value.value / 1e7  # 100 ns units


def awake_clock() -> float:
    """Seconds of *awake* time since boot (Windows) or ``time.monotonic()``."""
    seconds = _unbiased_seconds()
    return time.monotonic() if seconds is None else seconds
