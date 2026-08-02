"""Pure sync-engine helpers: scan the data root, diff against a manifest, plan.

No network and no backend imports here — everything is testable against tmp
dirs. The manifest is a JSON sidecar (one per backend, under ``<data_root>/.sync``)
mapping each synced file to the size/mtime/md5 we last pushed and the backend's
remote id, so a re-run can skip unchanged files without re-hashing gigabytes:
size+mtime match -> skip; mismatch -> hash and compare before deciding to upload.

Push-only by design (local is the source of truth). Files that vanish locally
are reported as ``orphan`` but never deleted remotely.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

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


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def load_manifest(path: Path) -> dict[str, Any]:
    """Load a manifest, or a fresh empty one if the file doesn't exist/parses badly."""
    empty: dict[str, Any] = {"version": MANIFEST_VERSION, "files": {}}
    path = Path(path)
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return empty
    if not isinstance(data, dict) or not isinstance(data.get("files"), dict):
        return empty
    data.setdefault("version", MANIFEST_VERSION)
    return data


def save_manifest(path: Path, manifest: Mapping[str, Any]) -> None:
    """Write the manifest atomically (tmp file + replace) so a crash mid-push
    never leaves a truncated sidecar behind."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
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
def build_plan(
    local: Mapping[str, FileStat],
    manifest: Mapping[str, Any],
    *,
    hasher: Callable[[str], str],
) -> list[PlanItem]:
    """Diff the local scan against the manifest into an ordered action plan.

    ``hasher`` maps a relpath to its md5 and is only called for files whose
    size/mtime moved (the expensive path is opt-in, and tests inject a fake).
    """
    entries: Mapping[str, Any] = manifest.get("files", {})
    plan: list[PlanItem] = []
    for relpath in sorted(local):
        stat = local[relpath]
        prev = entries.get(relpath)
        if prev is None:
            plan.append(PlanItem(relpath, ACTION_UPLOAD, "new", stat.size))
            continue
        if stat.size == prev.get("size") and stat.mtime_ns == prev.get("mtime_ns"):
            plan.append(PlanItem(relpath, ACTION_SKIP, "unchanged", stat.size))
            continue
        md5 = hasher(relpath)
        if md5 == prev.get("md5"):
            plan.append(
                PlanItem(relpath, ACTION_TOUCH, "content identical", stat.size, md5)
            )
        else:
            plan.append(PlanItem(relpath, ACTION_UPLOAD, "changed", stat.size, md5))
    for relpath in sorted(set(entries) - set(local)):
        plan.append(PlanItem(relpath, ACTION_ORPHAN, "missing locally"))
    return plan


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


def human_size(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
