"""Same-user, machine-wide file locks for jobs and canonical resources."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator, Sequence

from .model import ResourceClaim, utc_now


class LockUnavailable(RuntimeError):
    def __init__(self, resource_id: str, holder: dict | None = None) -> None:
        self.resource_id = resource_id
        self.holder = holder
        detail = f"; holder={holder}" if holder else ""
        super().__init__(f"resource is already locked: {resource_id}{detail}")


def default_state_root() -> Path:
    override = os.environ.get("DATACLI_STATE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        base = str(Path.home() / "AppData" / "Local")
    return Path(base) / "datacli"


def canonical_path(path: str | os.PathLike[str]) -> str:
    value = os.path.realpath(os.path.abspath(os.fspath(path)))
    return os.path.normcase(value).casefold()


def path_resource(path: str | os.PathLike[str]) -> str:
    return f"path:{canonical_path(path)}"


def _lock_key(resource_id: str) -> str:
    return hashlib.sha256(resource_id.encode("utf-8")).hexdigest()


class _OsFileLock:
    """One-byte shared/exclusive lock held by an open OS file handle."""

    def __init__(self, path: Path, *, exclusive: bool) -> None:
        self.path = path
        self.exclusive = exclusive
        self.file: BinaryIO | None = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b", buffering=0)
        if os.name == "nt":
            return self._acquire_windows()
        return self._acquire_posix()

    def _acquire_windows(self) -> bool:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        class OVERLAPPED(ctypes.Structure):
            _fields_ = [
                ("Internal", ctypes.c_void_p),
                ("InternalHigh", ctypes.c_void_p),
                ("Offset", wintypes.DWORD),
                ("OffsetHigh", wintypes.DWORD),
                ("hEvent", wintypes.HANDLE),
            ]

        self._overlapped = OVERLAPPED()  # type: ignore[attr-defined]
        assert self.file is not None
        handle = msvcrt.get_osfhandle(self.file.fileno())
        flags = 0x00000001  # LOCKFILE_FAIL_IMMEDIATELY
        if self.exclusive:
            flags |= 0x00000002  # LOCKFILE_EXCLUSIVE_LOCK
        ok = ctypes.windll.kernel32.LockFileEx(
            wintypes.HANDLE(handle),
            wintypes.DWORD(flags),
            0,
            1,
            0,
            ctypes.byref(self._overlapped),  # type: ignore[attr-defined]
        )
        if not ok:
            self.file.close()
            self.file = None
        return bool(ok)

    def _acquire_posix(self) -> bool:
        import fcntl

        assert self.file is not None
        try:
            mode = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH  # type: ignore[attr-defined]
            fcntl.flock(  # type: ignore[attr-defined]
                self.file.fileno(), mode | fcntl.LOCK_NB  # type: ignore[attr-defined]
            )
            return True
        except BlockingIOError:
            self.file.close()
            self.file = None
            return False

    def release(self) -> None:
        if self.file is None:
            return
        file = self.file
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            handle = msvcrt.get_osfhandle(file.fileno())
            ctypes.windll.kernel32.UnlockFileEx(
                wintypes.HANDLE(handle),
                0,
                1,
                0,
                ctypes.byref(self._overlapped),  # type: ignore[attr-defined]
            )
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]
        file.close()
        self.file = None


@dataclass
class HeldLock:
    resource_id: str
    file_lock: _OsFileLock

    def release(self) -> None:
        self.file_lock.release()


class LockManager:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or default_state_root() / "locks").resolve()

    def _paths(self, resource_id: str) -> tuple[Path, Path]:
        key = _lock_key(resource_id)
        return self.root / f"{key}.lock", self.root / f"{key}.holder.json"

    def holder(self, resource_id: str) -> dict | None:
        _, metadata = self._paths(resource_id)
        try:
            value = json.loads(metadata.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def acquire(
        self,
        claim: ResourceClaim,
        *,
        owner: dict | None = None,
        wait_seconds: float = 0,
    ) -> HeldLock:
        lock_path, metadata_path = self._paths(claim.resource_id)
        deadline = time.monotonic() + wait_seconds
        while True:
            primitive = _OsFileLock(lock_path, exclusive=claim.mode == "exclusive")
            if primitive.acquire():
                if owner is not None and claim.mode == "exclusive":
                    metadata_path.parent.mkdir(parents=True, exist_ok=True)
                    metadata_path.write_text(
                        json.dumps(
                            {
                                **owner,
                                "resource_id": claim.resource_id,
                                "mode": claim.mode,
                                "acquired_at": utc_now(),
                            },
                            sort_keys=True,
                        ),
                        encoding="utf-8",
                    )
                    with contextlib.suppress(OSError):
                        os.chmod(metadata_path, 0o600)
                return HeldLock(claim.resource_id, primitive)
            if time.monotonic() >= deadline:
                raise LockUnavailable(claim.resource_id, self.holder(claim.resource_id))
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))

    @contextlib.contextmanager
    def acquire_many(
        self,
        claims: Sequence[ResourceClaim],
        *,
        owner: dict | None = None,
        wait_seconds: float = 0,
    ) -> Iterator[tuple[HeldLock, ...]]:
        combined: dict[str, ResourceClaim] = {}
        for claim in claims:
            prior = combined.get(claim.resource_id)
            if prior is None or claim.mode == "exclusive":
                combined[claim.resource_id] = claim
        held: list[HeldLock] = []
        try:
            for claim in sorted(combined.values(), key=lambda x: x.resource_id):
                held.append(self.acquire(claim, owner=owner, wait_seconds=wait_seconds))
            yield tuple(held)
        finally:
            for item in reversed(held):
                item.release()
