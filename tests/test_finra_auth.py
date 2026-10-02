"""finra.auth: credential lookup, masking, and the FIP token cache."""

from __future__ import annotations

import base64
import logging
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from finra import auth  # noqa: E402
from finra.errors import AuthError, CredentialsError  # noqa: E402

NO_REGISTRY = lambda name: ""  # noqa: E731


class _Response:
    def __init__(self, status: int, body=None, headers=None) -> None:
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class _Session:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url, params=None, timeout=None, json=None, headers=None):
        self.calls.append(
            {"url": url, "params": params, "timeout": timeout, "headers": headers}
        )
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _token_response(token: str = "tok-1", expires_in: float = 3600) -> _Response:
    return _Response(200, {"access_token": token, "expires_in": expires_in})


# --------------------------------------------------------------------------- #
# lookup + credentials
# --------------------------------------------------------------------------- #
def test_process_env_wins_over_registry() -> None:
    seen: list[str] = []

    def registry(name: str) -> str:
        seen.append(name)
        return "from-registry"

    assert auth.read_user_env("X", {"X": " from-env "}, registry=registry) == "from-env"
    assert seen == []
    assert auth.read_user_env("X", {}, registry=registry) == "from-registry"
    assert seen == ["X"]


def test_from_env_both_neither_and_half() -> None:
    env = {auth.CLIENT_ID_VAR: "id-1234", auth.SECRET_VAR: "s3cret"}
    creds = auth.Credentials.from_env(env, registry=NO_REGISTRY)
    assert creds == auth.Credentials("id-1234", "s3cret")

    assert auth.Credentials.from_env({}, registry=NO_REGISTRY) is None

    with pytest.raises(
        CredentialsError, match="FINRA_CLIENT_ID is set but FINRA_API_KEY is not"
    ):
        auth.Credentials.from_env({auth.CLIENT_ID_VAR: "id"}, registry=NO_REGISTRY)
    with pytest.raises(
        CredentialsError, match="FINRA_API_KEY is set but FINRA_CLIENT_ID is not"
    ):
        auth.Credentials.from_env({auth.SECRET_VAR: "s"}, registry=NO_REGISTRY)


def test_from_env_falls_back_to_registry_per_variable() -> None:
    registry = {auth.SECRET_VAR: "reg-secret"}.get
    creds = auth.Credentials.from_env(
        {auth.CLIENT_ID_VAR: "id"}, registry=lambda n: registry(n, "")
    )
    assert creds == auth.Credentials("id", "reg-secret")


def test_credentials_never_show_the_secret() -> None:
    creds = auth.Credentials("client-abcd", "topsecret")
    assert "topsecret" not in repr(creds) and "topsecret" not in str(creds)
    assert repr(creds) == "Credentials(client_id=****abcd)"
    raw = base64.b64decode(creds.basic_authorization().split(" ", 1)[1]).decode()
    assert raw == "client-abcd:topsecret"


def test_mask() -> None:
    assert auth.mask("") == ""
    assert auth.mask("abc") == "****"
    assert auth.mask("abcdefgh") == "****efgh"


def test_read_windows_user_env_is_empty_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(auth.os, "name", "posix")
    assert auth.read_windows_user_env("ANYTHING") == ""


# --------------------------------------------------------------------------- #
# token provider
# --------------------------------------------------------------------------- #
def _provider(session, *, clock, skew=60.0) -> auth.TokenProvider:
    return auth.TokenProvider(
        session,
        auth.Credentials("id", "secret"),
        clock=clock,
        skew=skew,
        sleep=lambda _w: None,
    )


def test_token_is_fetched_once_and_cached() -> None:
    now = [1000.0]
    session = _Session(_token_response("tok-1", 3600))
    provider = _provider(session, clock=lambda: now[0])

    assert provider.seconds_remaining() is None
    assert provider.token() == "tok-1"
    assert provider.token() == "tok-1"
    assert len(session.calls) == 1 and provider.refreshes == 1
    assert provider.seconds_remaining() == 3600.0

    call = session.calls[0]
    assert call["url"] == auth.TOKEN_URL
    assert call["params"] == {"grant_type": "client_credentials"}
    assert call["headers"]["Authorization"].startswith("Basic ")


def test_token_refreshes_inside_the_skew_window() -> None:
    now = [0.0]
    session = _Session(_token_response("tok-1", 100), _token_response("tok-2", 100))
    provider = _provider(session, clock=lambda: now[0], skew=10)

    assert provider.token() == "tok-1"
    now[0] = 89.0  # 11s left > skew: reuse
    assert provider.token() == "tok-1"
    now[0] = 91.0  # 9s left <= skew: refresh
    assert provider.token() == "tok-2"
    assert provider.refreshes == 2


def test_invalidate_forces_a_new_token() -> None:
    session = _Session(_token_response("tok-1"), _token_response("tok-2"))
    provider = _provider(session, clock=lambda: 0.0)
    assert provider.token() == "tok-1"
    provider.invalidate()
    assert provider.seconds_remaining() is None
    assert provider.token() == "tok-2"


def test_rejected_credentials_raise_auth_error_without_body() -> None:
    session = _Session(
        _Response(401, {"error": "invalid_client"}, {"finra-api-request-id": "req-9"})
    )
    provider = _provider(session, clock=lambda: 0.0)
    with pytest.raises(AuthError) as info:
        provider.token()
    assert info.value.status == 401 and info.value.request_id == "req-9"
    assert "invalid_client" not in str(info.value)


def test_malformed_token_body_raises_auth_error() -> None:
    session = _Session(_Response(200, {"token_type": "Bearer"}))
    with pytest.raises(AuthError, match="not the expected JSON"):
        _provider(session, clock=lambda: 0.0).token()
    session = _Session(_Response(200, ValueError("no json")))
    with pytest.raises(AuthError, match="not the expected JSON"):
        _provider(session, clock=lambda: 0.0).token()


def test_transient_fip_failure_is_retried() -> None:
    session = _Session(_Response(503), _token_response("tok-1"))
    provider = _provider(session, clock=lambda: 0.0)
    assert provider.token() == "tok-1"
    assert len(session.calls) == 2


def test_nothing_secret_reaches_the_log(caplog: pytest.LogCaptureFixture) -> None:
    log = logging.getLogger("test_finra_auth")
    session = _Session(_Response(500), _token_response("tok-secret-value", 7200))
    provider = auth.TokenProvider(
        session,
        auth.Credentials("id", "the-secret"),
        clock=lambda: 0.0,
        log=log,
        sleep=lambda _w: None,
    )
    with caplog.at_level(logging.DEBUG, logger="test_finra_auth"):
        provider.token()
    assert "FINRA token obtained" in caplog.text
    assert "the-secret" not in caplog.text and "tok-secret-value" not in caplog.text
