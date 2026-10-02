"""Credentials and bearer tokens for the FINRA Query API.

FINRA issues an API client id and secret per account. The pair is exchanged at
the FINRA Identity Platform (FIP) for a short-lived bearer token using the
OAuth2 client-credentials grant: HTTP Basic ``client_id:client_secret`` on a
POST, JSON back with ``access_token`` and ``expires_in`` seconds.

The pair is read from ``FINRA_CLIENT_ID`` and ``FINRA_API_KEY`` (the secret).
Each variable is looked up in the process environment first and then in the
Windows user environment (``HKCU\\Environment``), the same two-step rule the
EODHD key uses: a shell or IDE started before the variable was set never sees
it in its own environment, while a fresh process and the scheduled task do.

Nothing here logs, prints or stores a secret or a token. :class:`Credentials`
masks itself in ``repr``; the token lives only inside :class:`TokenProvider`.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from finra.errors import AuthError, CredentialsError

CLIENT_ID_VAR = "FINRA_CLIENT_ID"
SECRET_VAR = "FINRA_API_KEY"
TOKEN_URL = "https://ews.fip.finra.org/fip/rest/ews/oauth2/access_token"
DEFAULT_SKEW_SECONDS = 60.0
DEFAULT_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# lookup
# --------------------------------------------------------------------------- #
def read_windows_user_env(name: str) -> str:
    """``name`` from the Windows user environment, or ``""`` (also off Windows)."""
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as env_key:
            value, _ = winreg.QueryValueEx(env_key, name)
            return str(value).strip()
    except OSError:
        return ""


def read_user_env(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    registry: Callable[[str], str] | None = None,
) -> str:
    """The process environment first, then the Windows user environment.

    ``registry`` defaults to :func:`read_windows_user_env`, looked up at call
    time so a test can replace the module attribute.
    """
    source = os.environ if env is None else env
    value = str(source.get(name, "")).strip()
    lookup = read_windows_user_env if registry is None else registry
    return value or lookup(name)


def mask(value: str, keep: int = 4) -> str:
    """``****abcd`` -- enough to recognise a value, never enough to use it."""
    if not value:
        return ""
    return "****" + value[-keep:] if len(value) > keep else "****"


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #
@dataclass(frozen=True, repr=False)
class Credentials:
    """An API client id + secret. ``repr`` and ``str`` never show the secret."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        return f"Credentials(client_id={mask(self.client_id)})"

    __str__ = __repr__

    def basic_authorization(self) -> str:
        """The ``Authorization`` header value for the FIP token request."""
        raw = f"{self.client_id}:{self.client_secret}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        registry: Callable[[str], str] | None = None,
    ) -> "Credentials | None":
        """Both variables set -> credentials; neither -> ``None``.

        Raises:
            CredentialsError: when exactly one of the two variables is set,
                naming the missing one. A half-configured pair must be visible,
                not silently anonymous.
        """
        client_id = read_user_env(CLIENT_ID_VAR, env, registry=registry)
        secret = read_user_env(SECRET_VAR, env, registry=registry)
        if not client_id and not secret:
            return None
        if not client_id or not secret:
            missing = SECRET_VAR if client_id else CLIENT_ID_VAR
            present = CLIENT_ID_VAR if client_id else SECRET_VAR
            raise CredentialsError(f"{present} is set but {missing} is not")
        return cls(client_id, secret)


# --------------------------------------------------------------------------- #
# tokens
# --------------------------------------------------------------------------- #
class TokenProvider:
    """One cached bearer token, refreshed before it expires.

    ``clock`` is monotonic seconds (injectable for tests); a token is reused
    while more than ``skew`` seconds of its lifetime remain. The API client
    calls :meth:`invalidate` after a 401 so the next call fetches a fresh one.
    """

    def __init__(
        self,
        session: Any,
        credentials: Credentials,
        *,
        clock: Callable[[], float] = time.monotonic,
        skew: float = DEFAULT_SKEW_SECONDS,
        timeout: float = DEFAULT_TIMEOUT,
        token_url: str = TOKEN_URL,
        log: logging.Logger | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._session = session
        self._credentials = credentials
        self._clock = clock
        self._skew = skew
        self._timeout = timeout
        self._token_url = token_url
        self._log = log
        self._sleep = sleep
        self._token: str | None = None
        self._expires_at: float = 0.0
        self.refreshes = 0  # how many tokens were fetched; handy for tests/status

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    def token(self) -> str:
        if self._token is not None and self._expires_at - self._clock() > self._skew:
            return self._token
        self._token, self._expires_at = self._fetch()
        self.refreshes += 1
        return self._token

    def invalidate(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def seconds_remaining(self) -> float | None:
        """Lifetime left on the cached token, or ``None`` when there is none."""
        if self._token is None:
            return None
        return max(0.0, self._expires_at - self._clock())

    def _fetch(self) -> tuple[str, float]:
        from _http import request_with_retry  # type: ignore[import-not-found]

        response = request_with_retry(
            self._session,
            "POST",
            self._token_url,
            params={"grant_type": "client_credentials"},
            headers={
                "Authorization": self._credentials.basic_authorization(),
                "Accept": "application/json",
            },
            timeout=self._timeout,
            log=self._log,
            label="FIP token",
            sleep=self._sleep,
        )
        request_id = _header(response, "finra-api-request-id")
        if response.status_code != 200:
            raise AuthError(
                "FINRA rejected the client credentials",
                status=response.status_code,
                request_id=request_id,
            )
        try:
            body = response.json()
            token = str(body["access_token"])
            expires_in = float(body.get("expires_in", 0))
        except (ValueError, KeyError, TypeError, AttributeError):
            raise AuthError(
                "FINRA token response was not the expected JSON",
                status=response.status_code,
                request_id=request_id,
            ) from None
        if not token:
            raise AuthError("FINRA token response carried an empty token")
        if self._log is not None:
            self._log.info("FINRA token obtained, valid %.0fs", expires_in)
        return token, self._clock() + expires_in


def _header(response: Any, name: str) -> str | None:
    try:
        value = response.headers.get(name)
    except AttributeError:
        return None
    return str(value) if value else None
