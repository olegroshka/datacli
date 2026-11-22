"""The error vocabulary of the Alpaca access layer.

Callers catch what they mean: a missing or half-set key pair
(:class:`CredentialsError`) before any network call, a key the API rejects
(:class:`AuthError`), and the transport outcomes. Every error carries the HTTP
status when there is one and Alpaca's own message; none carries a header, a
key or a full response body.
"""

from __future__ import annotations


class AlpacaError(Exception):
    """Base class; ``status`` is the HTTP status when known."""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status

    def __str__(self) -> str:
        text = super().__str__()
        return f"{text} (HTTP {self.status})" if self.status is not None else text


class CredentialsError(AlpacaError):
    """The key pair is missing or only half set."""


class AuthError(AlpacaError):
    """The API rejected the key pair (HTTP 401 / 403)."""


class RateLimited(AlpacaError):
    """HTTP 429 after the bounded retries were spent."""


class ServerError(AlpacaError):
    """HTTP 5xx after the bounded retries were spent."""


class ApiError(AlpacaError):
    """Any other failure: unexpected status, non-JSON body, malformed payload."""


class QuietWindowActive(AlpacaError):
    """A request would start inside a configured quiet window (the shared key is in use)."""
