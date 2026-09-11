"""The shared bounded-retry GET used by the per-ticker EODHD fetchers."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
import requests

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS_EODHD = _REPO_ROOT / "eodhd"
if str(_SCRIPTS_EODHD) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_EODHD))

import _http  # type: ignore  # noqa: E402


class _Response:
    def __init__(self, status: int, headers: dict | None = None) -> None:
        self.status_code = status
        self.headers = headers or {}


class _Session:
    """Yields the scripted outcomes in order: an Exception is raised,
    anything else is returned."""

    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, {"params": params, "timeout": timeout}))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_connection_errors_are_retried_then_succeed() -> None:
    session = _Session(
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        _Response(200),
    )
    waits: list[float] = []

    response = _http.get_with_retry(
        session, "u", params={"a": 1}, timeout=7, sleep=waits.append
    )

    assert response.status_code == 200
    assert len(session.calls) == 3
    assert session.calls[0][1] == {"params": {"a": 1}, "timeout": 7}
    assert waits == [2.0, 4.0]


def test_server_errors_and_429_are_retried_and_last_response_returned() -> None:
    session = _Session(
        _Response(503),
        _Response(429, {"Retry-After": "9"}),
        _Response(502),
    )
    waits: list[float] = []

    response = _http.get_with_retry(
        session, "u", timeout=1, attempts=3, sleep=waits.append
    )

    assert response.status_code == 502  # exhausted: caller sees the last status
    assert waits == [2.0, 9.0]  # Retry-After beats the backoff when larger


def test_other_4xx_are_returned_untouched() -> None:
    session = _Session(_Response(404))
    waits: list[float] = []
    response = _http.get_with_retry(session, "u", timeout=1, sleep=waits.append)
    assert response.status_code == 404 and waits == [] and len(session.calls) == 1


def test_exhausted_connection_errors_reraise_last_error(caplog) -> None:
    session = _Session(
        requests.ConnectionError("one"),
        requests.ConnectionError("two"),
    )
    log = logging.getLogger("test_http")
    with caplog.at_level(logging.WARNING, logger="test_http"):
        with pytest.raises(requests.ConnectionError, match="two"):
            _http.get_with_retry(
                session,
                "u",
                timeout=1,
                attempts=2,
                sleep=lambda _w: None,
                log=log,
                label="AAA.LSE",
            )
    assert "retry 1/2 AAA.LSE" in caplog.text


def test_backoff_is_capped() -> None:
    session = _Session(*([_Response(500)] * 6))
    waits: list[float] = []
    _http.get_with_retry(
        session,
        "u",
        timeout=1,
        attempts=6,
        backoff=10,
        max_backoff=25,
        sleep=waits.append,
    )
    assert waits == [10, 20, 25, 25, 25]


def test_sleep_defaults_to_time_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr(_http.time, "sleep", slept.append)
    session = _Session(_Response(500), _Response(200))
    assert _http.get_with_retry(session, "u", timeout=1).status_code == 200
    assert slept == [2.0]
