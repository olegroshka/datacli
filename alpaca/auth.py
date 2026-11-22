"""The Alpaca key pair.

Alpaca authenticates market data requests with two headers,
``APCA-API-KEY-ID`` and ``APCA-API-SECRET-KEY``. The pair is read from
``APCA_API_KEY_ID`` and ``APCA_API_SECRET_KEY``, each from the process
environment first and then from the Windows user environment, by the same
two-step rule as the FINRA and EODHD keys (:func:`finra.auth.read_user_env`).

The pair is shared with other sessions on this machine (the paper book's live
bars), which is why :mod:`alpaca.quiet` exists. Nothing here logs, prints or
stores a key: :class:`Credentials` masks itself in ``repr``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from alpaca.errors import CredentialsError
from finra.auth import mask, read_user_env

KEY_ID_VAR = "APCA_API_KEY_ID"
SECRET_VAR = "APCA_API_SECRET_KEY"


@dataclass(frozen=True, repr=False)
class Credentials:
    """An Alpaca key id and secret. ``repr`` and ``str`` never show the secret."""

    key_id: str
    secret: str

    def __repr__(self) -> str:
        return f"Credentials(key_id={mask(self.key_id)})"

    __str__ = __repr__

    def headers(self) -> dict[str, str]:
        """The two authentication headers of every data API request."""
        return {"APCA-API-KEY-ID": self.key_id, "APCA-API-SECRET-KEY": self.secret}

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        registry: Callable[[str], str] | None = None,
    ) -> "Credentials":
        """Both variables set -> credentials.

        Raises:
            CredentialsError: when either variable is missing, naming it. The
                data API has no anonymous tier, so an absent pair is an error.
        """
        key_id = read_user_env(KEY_ID_VAR, env, registry=registry)
        secret = read_user_env(SECRET_VAR, env, registry=registry)
        missing = [
            name
            for name, value in ((KEY_ID_VAR, key_id), (SECRET_VAR, secret))
            if not value
        ]
        if missing:
            raise CredentialsError(f"{' and '.join(missing)} not set")
        return cls(key_id, secret)


def available(
    env: Mapping[str, str] | None = None,
    *,
    registry: Callable[[str], str] | None = None,
) -> bool:
    """Whether a full key pair is configured (never raises, never reveals it)."""
    try:
        Credentials.from_env(env, registry=registry)
    except CredentialsError:
        return False
    return True
