"""Storage backend ABC + the built-in ``local`` backend and backend factory.

A backend only needs to authenticate and upload — the diffing/skipping brains
live in ``engine.py``. Keeping the surface this small is what makes new
providers cheap: ``gdrive`` is the first cloud backend, ``local`` copies to
another path (NAS / USB / a Google Drive Desktop folder) and doubles as the
no-network test double.

Settings come from the git-ignored ``datacli.toml`` ``[sync]`` section, flat
string keys only (the minimal TOML writer in ``eodhd/config.py`` can't nest):

    [sync]
    backend = "gdrive"                   # or "local"
    remote_root = "datacli/eodhd"        # gdrive folder path under My Drive
    gdrive_client_secrets = "C:/.../client_secret_....json"
    # gdrive_token = "..."               # default: ~/.datacli/tokens/gdrive.json
    # local_dest = "D:/backup/eodhd"     # for backend = "local"
"""

from __future__ import annotations

import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Mapping

KNOWN_BACKENDS = ("gdrive", "local")


class SyncConfigError(RuntimeError):
    """Missing/invalid [sync] configuration — message is user-facing."""


class SyncAuthError(RuntimeError):
    """Not signed in and the caller asked for a non-interactive run."""


class StorageBackend(ABC):
    """Minimal push surface a sync target must implement."""

    name: str = ""

    @abstractmethod
    def describe(self) -> str:
        """One-line target description for tables/plans (no network)."""

    @abstractmethod
    def ensure_auth(self, interactive: bool = True) -> str:
        """Make the backend ready to upload; return an account/target label.

        May pop a browser for an OAuth consent flow when ``interactive`` — with
        ``interactive=False`` it must instead raise :class:`SyncAuthError` if
        there is no cached credential.
        """

    @abstractmethod
    def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
        """Upload one file, creating remote parents as needed; return remote id.

        ``remote_id`` (from the manifest) updates the existing remote file in
        place when possible, so history/identity is preserved on providers that
        version files.
        """


class LocalBackend(StorageBackend):
    """Copy files under a destination directory (mirrors the relpath tree)."""

    name = "local"

    def __init__(self, dest_root: Path) -> None:
        self.dest_root = Path(dest_root)

    def describe(self) -> str:
        return f"local:{self.dest_root}"

    def ensure_auth(self, interactive: bool = True) -> str:
        self.dest_root.mkdir(parents=True, exist_ok=True)
        return str(self.dest_root)

    def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
        dest = self.dest_root / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(local, dest)
        return relpath


def make_backend(settings: Mapping[str, object]) -> StorageBackend:
    """Build the configured backend from the flat ``[sync]`` settings dict."""
    name = str(settings.get("backend") or "gdrive")
    if name == "local":
        dest = settings.get("local_dest")
        if not dest:
            raise SyncConfigError(
                "backend 'local' needs [sync] local_dest — set it with:\n"
                "  config set sync-local-dest D:/backup/eodhd"
            )
        return LocalBackend(Path(str(dest)))
    if name == "gdrive":
        from storage.gdrive import GDriveBackend  # lazy: google deps optional

        return GDriveBackend(
            client_secrets=settings.get("gdrive_client_secrets"),
            token_path=settings.get("gdrive_token"),
            remote_root=str(settings.get("remote_root") or "datacli/eodhd"),
        )
    raise SyncConfigError(
        f"unknown sync backend '{name}'. Known: {', '.join(KNOWN_BACKENDS)}"
    )
