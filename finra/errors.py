"""The error vocabulary of the FINRA access layer.

One small hierarchy so callers can catch what they mean: a configuration
problem (:class:`CredentialsError`) before any network call, a credential the
identity platform rejects (:class:`AuthError`), a dataset the credential is not
entitled to (:class:`EntitlementError`), and the transport-level outcomes.
Every error carries the HTTP status and FINRA's request id when there is one;
none of them ever carries a secret, a token or a response body.
"""

from __future__ import annotations


class FinraError(Exception):
    """Base class; ``status`` and ``request_id`` are set when known."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        request_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id

    def __str__(self) -> str:
        text = super().__str__()
        extras = []
        if self.status is not None:
            extras.append(f"HTTP {self.status}")
        if self.request_id:
            extras.append(f"request {self.request_id}")
        return f"{text} ({', '.join(extras)})" if extras else text


class CredentialsError(FinraError):
    """The environment holds an unusable credential pair (e.g. only one var)."""


class AuthError(FinraError):
    """FINRA's identity platform rejected the credentials or the token."""


class EntitlementError(FinraError):
    """The API refused the dataset: anonymous where auth is needed, or no entitlement."""


class NotFound(FinraError):
    """Unknown group / dataset / resource."""


class RateLimited(FinraError):
    """HTTP 429 after the bounded retries were spent."""


class ServerError(FinraError):
    """HTTP 5xx after the bounded retries were spent."""


class TooManyRows(FinraError):
    """A query would need to page past FINRA's offset ceiling; narrow the filter."""


class ApiError(FinraError):
    """Any other API failure: unexpected status, non-JSON body, malformed payload."""


class CdnError(FinraError):
    """The public file endpoint failed in a way that is not "no such day"."""


class DailyFileFormatError(FinraError):
    """A daily file broke the strict layout; the message names line and rule."""
