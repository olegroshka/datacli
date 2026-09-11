"""Pure sync-engine helpers: scan the data root, diff against a manifest, plan.

No network and no backend imports here — everything is testable against tmp
dirs. The manifest is a JSON sidecar (one per backend, under ``<data_root>/.sync``)
mapping each synced file to the size/mtime/md5 we last pushed and the backend's
remote id, so a re-run can skip unchanged files without re-hashing gigabytes:
size+mtime match -> skip; mismatch -> hash and compare before deciding to upload.

Push-only by design (local is the source of truth). Files that vanish locally
are reported as ``orphan`` but never deleted remotely.

Two lessons from the 2026-09-11 incident are baked in here:

* A manifest that exists but cannot be parsed raises :class:`ManifestError`
  instead of quietly becoming "first push ever". The old behaviour turned a
  lost manifest into a full re-upload that duplicated every file on Drive.
  ``save_manifest`` also fsyncs before the atomic rename, because the loss
  followed an unclean shutdown.
* :func:`rebuild_manifest` reconstructs the manifest from a backend listing
  (remote id + md5 per path), so recovery comes from the remote tree rather
  than from re-uploading 8.7 GB.

Upload order is by value: root-level and sidecar metadata first, then the most
recently modified files, so a push that dies half-way has still moved today's
data rather than the 2019 news backlog. See :func:`upload_order_key`.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

MANIFEST_VERSION = 1

# What a "whole data root" push includes: the materialized datasets and their
# sidecars — not the per-firm payload caches (13k+ tiny .gz files on Drive is a
# rate-limited slog; they are re-derivable from the API).
DEFAULT_INCLUDE = ("*.parquet", "*.csv", "*.json", "*.md")
DEFAULT_EXCLUDE_DIRS = frozenset({"cache", "probe_cache", ".sync"})

ACTION_UPLOAD = "upload"
ACTION_SKIP = "skip"
ACTION_TOUCH = "touch"  # content identical, only size/mtime metadata to refresh
ACTION_ORPHAN = "orphan"  # in manifest but gone locally (reported, never deleted)

#: Sidecar/metadata files are pushed before bulk parquet (see upload_order_key).
METADATA_SUFFIXES = frozenset({".json", ".csv", ".md"})


class ManifestError(RuntimeError):
    """The manifest file exists but cannot be used. Never treated as empty."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"sync manifest unreadable: {path} ({reason})")
        self.path = Path(path)
        self.reason = reason


@dataclass(frozen=True)
class FileStat:
    """Identity of one local file, relative to the data root (POSIX slashes)."""

    relpath: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class PlanItem:
    """One planned action for a file. ``md5`` is set when hashing already ran."""

    relpath: str
    action: str
    reason: str
    size: int = 0
    md5: str | None = None


@dataclass(frozen=True)
class RemoteFile:
    """One file the backend holds for a relpath (there may be duplicates)."""

    remote_id: str
    md5: str | None
    size: int | None
    modified: str | None = None  # backend timestamp, ISO-ish, for tie-breaks


def scan_local(
    root: Path,
    *,
    include: Iterable[str] = DEFAULT_INCLUDE,
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS,
) -> dict[str, FileStat]:
    """Walk ``root`` and stat every file matching the include globs.

    ``exclude_dirs`` prunes by directory *name* at any depth (cache dirs, the
    ``.sync`` manifest dir). Returns ``{relpath: FileStat}`` with POSIX-style
    relative paths so manifests are portable across OSes.
    """
    root = Path(root)
    include = tuple(include)
    found: dict[str, FileStat] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude_dirs)
        for name in sorted(filenames):
            if not any(fnmatch.fnmatch(name, pat) for pat in include):
                continue
            path = Path(dirpath) / name
            stat = path.stat()
            relpath = path.relative_to(root).as_posix()
            found[relpath] = FileStat(relpath, stat.st_size, stat.st_mtime_ns)
    return found


def md5_file(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.md5()
    with Path(path).open("rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def is_metadata(relpath: str) -> bool:
    """Root-level files (STATUS.*, _datacli_*) and .json/.csv/.md sidecars."""
    return "/" not in relpath or Path(relpath).suffix.lower() in METADATA_SUFFIXES


def _stamp(now: datetime | None) -> str:
    return (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> dict[str, Any]:
    """Load a manifest; absent -> fresh empty one; unreadable -> ManifestError.

    "Absent" and "unreadable" are deliberately different outcomes: an absent
    manifest is a legitimate first push, an unreadable one means state was
    lost and the caller must recover it (``sync reconcile``) rather than
    re-upload the world.
    """
    empty: dict[str, Any] = {"version": MANIFEST_VERSION, "files": {}}
    path = Path(path)
    if not path.exists():
        return empty
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ManifestError(path, f"cannot read: {exc}") from exc
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise ManifestError(path, f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        raise ManifestError(path, "unexpected shape: no 'files' mapping")
    data.setdefault("version", MANIFEST_VERSION)
    return data


def quarantine_manifest(path: Path, now: datetime | None = None) -> Path:
    """Move an unreadable manifest aside (``<name>.corrupt-<stamp>``)."""
    path = Path(path)
    target = path.with_name(f"{path.name}.corrupt-{_stamp(now)}")
    path.replace(target)
    return target


def backup_manifest(path: Path, now: datetime | None = None) -> Path | None:
    """Copy the current manifest to ``<name>.bak-<stamp>`` before a rebuild."""
    path = Path(path)
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.bak-{_stamp(now)}")
    shutil.copy2(path, target)
    return target


def save_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Write the manifest durably: tmp file, fsync, then atomic replace.

    A crash mid-push never leaves a truncated sidecar behind, and (unlike a
    bare rename) an unclean power-off after the rename cannot leave a zeroed
    file either.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps(manifest, indent=2, sort_keys=True))
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def record_push(
    manifest: dict[str, Any],
    stat: FileStat,
    *,
    md5: str,
    remote_id: str,
    now: datetime | None = None,
) -> None:
    """Record a successful upload (or metadata refresh) for one file in-place."""
    now = now or datetime.now(timezone.utc)
    manifest.setdefault("files", {})[stat.relpath] = {
        "size": stat.size,
        "mtime_ns": stat.mtime_ns,
        "md5": md5,
        "remote_id": remote_id,
        "uploaded_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


# --------------------------------------------------------------------------- #
# planning
# --------------------------------------------------------------------------- #
def upload_order_key(item: PlanItem, stat: FileStat | None) -> tuple[int, int, str]:
    """Metadata first, then newest-modified first, then path for stability."""
    mtime = stat.mtime_ns if stat is not None else 0
    return (0 if is_metadata(item.relpath) else 1, -mtime, item.relpath)


def build_plan(
    local: Mapping[str, FileStat],
    manifest: Mapping[str, Any],
    *,
    hasher: Callable[[str], str],
) -> list[PlanItem]:
    """Diff the local scan against the manifest into an ordered action plan.

    ``hasher`` maps a relpath to its md5 and is only called for files whose
    size/mtime moved (the expensive path is opt-in, and tests inject a fake).

    Uploads come first, ordered by :func:`upload_order_key`; then touches and
    skips by path, then orphans.
    """
    entries: Mapping[str, Any] = manifest.get("files", {})
    uploads: list[PlanItem] = []
    rest: list[PlanItem] = []
    for relpath in sorted(local):
        stat = local[relpath]
        prev = entries.get(relpath)
        if prev is None:
            uploads.append(PlanItem(relpath, ACTION_UPLOAD, "new", stat.size))
            continue
        if stat.size == prev.get("size") and stat.mtime_ns == prev.get("mtime_ns"):
            rest.append(PlanItem(relpath, ACTION_SKIP, "unchanged", stat.size))
            continue
        md5 = hasher(relpath)
        if md5 == prev.get("md5"):
            rest.append(
                PlanItem(relpath, ACTION_TOUCH, "content identical", stat.size, md5)
            )
        else:
            uploads.append(PlanItem(relpath, ACTION_UPLOAD, "changed", stat.size, md5))
    uploads.sort(key=lambda item: upload_order_key(item, local.get(item.relpath)))
    orphans = [
        PlanItem(relpath, ACTION_ORPHAN, "missing locally")
        for relpath in sorted(set(entries) - set(local))
    ]
    return uploads + rest + orphans


def summarize_plan(plan: Iterable[PlanItem]) -> dict[str, Any]:
    """Counts per action plus the total bytes the uploads will move."""
    counts = {a: 0 for a in (ACTION_UPLOAD, ACTION_SKIP, ACTION_TOUCH, ACTION_ORPHAN)}
    upload_bytes = 0
    for item in plan:
        counts[item.action] += 1
        if item.action == ACTION_UPLOAD:
            upload_bytes += item.size
    counts["upload_bytes"] = upload_bytes
    return counts


# --------------------------------------------------------------------------- #
# reconcile: rebuild the manifest from what the backend actually holds
# --------------------------------------------------------------------------- #
@dataclass
class RebuildReport:
    """What :func:`rebuild_manifest` found. ``duplicates`` lists, per relpath
    with more than one remote copy, the id kept and the ids not chosen."""

    matched: int = 0  # remote md5 == local md5 -> recorded as already pushed
    differs: int = 0  # remote exists, content differs -> next push updates it
    remote_only: int = 0  # on the backend, not local -> kept as orphan entries
    local_only: int = 0  # local, not on the backend -> next push creates it
    duplicates: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)

    @property
    def remote_paths(self) -> int:
        return self.matched + self.differs + self.remote_only

    def summary(self) -> str:
        return (
            f"{self.remote_paths} remote path(s): {self.matched} match local, "
            f"{self.differs} differ, {self.remote_only} remote-only; "
            f"{self.local_only} local-only; "
            f"{len(self.duplicates)} path(s) with duplicate remote copies"
        )


def choose_remote(
    candidates: Sequence[RemoteFile], local_md5: str | None
) -> RemoteFile:
    """Pick the copy to keep: the one matching local content, else the newest."""
    if local_md5:
        for candidate in candidates:
            if candidate.md5 == local_md5:
                return candidate
    return max(candidates, key=lambda c: c.modified or "")


def rebuild_manifest(
    local: Mapping[str, FileStat],
    remote: Mapping[str, Sequence[RemoteFile]],
    *,
    hasher: Callable[[str], str],
    now: datetime | None = None,
) -> tuple[dict[str, Any], RebuildReport]:
    """Build a manifest from a backend listing so nothing is re-created.

    For every remote path: if a copy's md5 equals the local file's, record it
    as pushed (a following plan skips it). If the content differs, record the
    remote id with a sentinel size/mtime so the next push *updates* that id.
    Remote-only paths are kept as entries (they show up as orphans). Nothing
    here touches the backend; duplicates are only reported.
    """
    manifest: dict[str, Any] = {"version": MANIFEST_VERSION, "files": {}}
    report = RebuildReport()
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for relpath in sorted(remote):
        candidates = list(remote[relpath])
        if not candidates:
            continue
        stat = local.get(relpath)
        local_md5 = hasher(relpath) if stat is not None else None
        chosen = choose_remote(candidates, local_md5)
        losers = tuple(
            c.remote_id for c in candidates if c.remote_id != chosen.remote_id
        )
        if losers:
            report.duplicates.append((relpath, chosen.remote_id, losers))
        if stat is not None and chosen.md5 and chosen.md5 == local_md5:
            report.matched += 1
            record_push(
                manifest,
                stat,
                md5=local_md5 or "",
                remote_id=chosen.remote_id,
                now=now,
            )
            continue
        if stat is None:
            report.remote_only += 1
        else:
            report.differs += 1
        manifest["files"][relpath] = {
            "size": chosen.size if chosen.size is not None else -1,
            "mtime_ns": 0,  # forces a hash+compare on the next plan
            "md5": chosen.md5 or "",
            "remote_id": chosen.remote_id,
            "uploaded_at": stamp,
        }
    report.local_only = len(set(local) - set(remote))
    return manifest, report


def human_size(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
