"""One bounded-retry HTTP GET for the EODHD fetchers.

Every per-ticker fetcher used to call ``session.get`` bare, so a single TCP
reset (``ConnectionResetError(10054)``) or ``RemoteDisconnected`` killed the
whole lane and, through the scheduler's fail-fast policy, the whole day.
:func:`get_with_retry` retries the transient failures a few times with a small
exponential backoff and then re-raises, so the caller can still decide what to
do (the fetchers skip the ticker and carry on).

Retried: ``requests.ConnectionError``, ``requests.Timeout``,
``ChunkedEncodingError`` and HTTP 429/500/502/503/504 (a 429 honours
``Retry-After`` when it is a small integer). Other 4xx responses are returned
untouched so the caller keeps its own status handling.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping

import requests

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
DEFAULT_ATTEMPTS = 4
DEFAULT_BACKOFF = 2.0
MAX_BACKOFF = 30.0

_RETRY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    requests.ConnectionError,
    requests.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def _delay(attempt: int, backoff: float, max_backoff: float) -> float:
    return min(max_backoff, backoff * (2 ** (attempt - 1)))


def _retry_after(response: Any, cap: float) -> float | None:
    try:
        value = response.headers.get("Retry-After")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return min(cap, max(0.0, seconds))


def get_with_retry(
    session: Any,
    url: str,
    *,
    params: Mapping[str, Any] | None = None,
    timeout: float,
    attempts: int = DEFAULT_ATTEMPTS,
    backoff: float = DEFAULT_BACKOFF,
    max_backoff: float = MAX_BACKOFF,
    log: logging.Logger | None = None,
    label: str = "",
    sleep: Callable[[float], None] | None = None,
) -> requests.Response:
    """``session.get`` with bounded retries on transient failures.

    Returns the last response (even a 5xx/429 one) once ``attempts`` is spent
    and re-raises the last connection error, so callers keep their existing
    ``status_code`` branches and gain an ``except requests.RequestException``.
    """
    if attempts < 1:
        raise ValueError("attempts must be >= 1")
    pause = time.sleep if sleep is None else sleep
    who = f" {label}" if label else ""
    for attempt in range(1, attempts + 1):
        try:
            response = session.get(url, params=params, timeout=timeout)
        except _RETRY_EXCEPTIONS as exc:
            if attempt == attempts:
                raise
            wait = _delay(attempt, backoff, max_backoff)
            if log is not None:
                log.warning(
                    "retry %d/%d%s in %.0fs: %s: %s",
                    attempt,
                    attempts,
                    who,
                    wait,
                    type(exc).__name__,
                    exc,
                )
            pause(wait)
            continue
        if response.status_code not in RETRY_STATUSES or attempt == attempts:
            return response
        wait = _delay(attempt, backoff, max_backoff)
        if response.status_code == 429:
            hinted = _retry_after(response, max_backoff * 2)
            if hinted is not None:
                wait = max(wait, hinted)
        if log is not None:
            log.warning(
                "retry %d/%d%s in %.0fs: HTTP %s",
                attempt,
                attempts,
                who,
                wait,
                response.status_code,
            )
        pause(wait)
    raise AssertionError("unreachable")  # pragma: no cover
