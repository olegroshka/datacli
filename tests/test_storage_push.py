"""Push/reconcile semantics that came out of the 2026-09-11 Drive incident.

All against the local backend or in-memory fakes: no network, no Drive.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from storage import engine  # noqa: E402
from storage.backends import LocalBackend  # noqa: E402
from storage.engine import FileStat, RemoteFile  # noqa: E402

pytest.importorskip("rich")
from storage.cli import execute_push, execute_reconcile  # noqa: E402


def _write(
    root: Path, relpath: str, content: bytes, *, mtime: float | None = None
) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    if mtime is not None:
        import os

        os.utime(path, (mtime, mtime))
    return path


class _Recording(LocalBackend):
    """Local backend that records every upload call (relpath, remote_id)."""

    def __init__(self, dest: Path) -> None:
        super().__init__(dest)
        self.uploads: list[tuple[str, str | None]] = []

    def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
        self.uploads.append((relpath, remote_id))
        return super().upload(local, relpath, remote_id)


# --------------------------------------------------------------------------- #
# manifest: loud loss, durable save
# --------------------------------------------------------------------------- #
def test_unreadable_manifest_fails_push_and_is_kept_aside(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "STATUS.json", b"{}")
    manifest_path = root / ".sync" / "local.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_bytes(b"\x00\x00\x00")  # what a zeroed file looks like
    backend = _Recording(tmp_path / "backup")
    lines: list[str] = []

    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 2
    assert backend.uploads == []  # nothing was uploaded against a lost manifest
    assert not manifest_path.exists()
    corrupt = list(manifest_path.parent.glob("local.json.corrupt-*"))
    assert len(corrupt) == 1
    assert any("unreadable" in line for line in lines)


def test_save_manifest_is_atomic_and_leaves_no_tmp(tmp_path: Path) -> None:
    path = tmp_path / ".sync" / "local.json"
    engine.save_manifest(path, {"version": 1, "files": {"a": {"size": 1}}})
    assert json.loads(path.read_text(encoding="utf-8"))["files"] == {"a": {"size": 1}}
    assert not path.with_suffix(".json.tmp").exists()


# --------------------------------------------------------------------------- #
# empty manifest + populated backend: rebuild, never duplicate
# --------------------------------------------------------------------------- #
def test_empty_manifest_against_existing_remote_tree_updates_not_creates(
    tmp_path: Path,
) -> None:
    root = tmp_path / "data"
    dest = tmp_path / "backup"
    _write(root, "STATUS.json", b"{}")
    _write(root, "index_ref/prices_daily.parquet", b"same-bytes")
    _write(root, "news/articles/2019-01-01.parquet", b"changed-locally")
    # the backend already holds all three (one of them with older content)
    _write(dest, "STATUS.json", b"{}")
    _write(dest, "index_ref/prices_daily.parquet", b"same-bytes")
    _write(dest, "news/articles/2019-01-01.parquet", b"old-remote-bytes")
    manifest_path = root / ".sync" / "local.json"
    backend = _Recording(dest)
    lines: list[str] = []

    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 0

    # exactly one upload: the file whose content differs, addressed by remote id
    assert backend.uploads == [
        ("news/articles/2019-01-01.parquet", "news/articles/2019-01-01.parquet")
    ]
    assert any("rebuilt it from" in line for line in lines)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {
        "STATUS.json",
        "index_ref/prices_daily.parquet",
        "news/articles/2019-01-01.parquet",
    }
    # and a second push has nothing left to do
    lines.clear()
    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 0
    assert "Everything in sync." in lines


def test_empty_manifest_against_empty_backend_is_a_first_push(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "STATUS.json", b"{}")
    backend = _Recording(tmp_path / "backup")
    lines: list[str] = []
    manifest_path = root / ".sync" / "local.json"

    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 0
    assert backend.uploads == [("STATUS.json", None)]
    assert any("first push" in line for line in lines)


def test_rebuild_manifest_prefers_md5_match_then_newest_and_reports_duplicates() -> (
    None
):
    local = {
        "a.parquet": FileStat("a.parquet", 3, 111),
        "b.parquet": FileStat("b.parquet", 3, 222),
        "c.parquet": FileStat("c.parquet", 3, 333),
    }
    remote = {
        # duplicate copies; the older one matches local content
        "a.parquet": [
            RemoteFile("a-new", "md5-other", 3, "2026-09-11T09:35:00Z"),
            RemoteFile("a-old", "md5-a", 3, "2026-08-31T10:00:00Z"),
        ],
        # duplicate copies; neither matches -> newest kept, next push updates it
        "b.parquet": [
            RemoteFile("b-1", "x", 3, "2026-08-20T00:00:00Z"),
            RemoteFile("b-2", "y", 3, "2026-09-11T00:00:00Z"),
        ],
        # remote only
        "gone.parquet": [RemoteFile("g", "z", 1, None)],
    }
    hashes = {"a.parquet": "md5-a", "b.parquet": "md5-b", "c.parquet": "md5-c"}
    now = datetime(2026, 9, 11, 12, 0, tzinfo=timezone.utc)

    manifest, report = engine.rebuild_manifest(
        local, remote, hasher=hashes.__getitem__, now=now
    )

    assert (report.matched, report.differs, report.remote_only, report.local_only) == (
        1,
        1,
        1,
        1,
    )
    assert report.duplicates == [
        ("a.parquet", "a-old", ("a-new",)),
        ("b.parquet", "b-2", ("b-1",)),
    ]
    assert manifest["files"]["a.parquet"]["remote_id"] == "a-old"
    assert manifest["files"]["a.parquet"]["mtime_ns"] == 111  # recorded as pushed
    assert manifest["files"]["b.parquet"]["remote_id"] == "b-2"
    assert manifest["files"]["b.parquet"]["mtime_ns"] == 0  # forces re-hash
    assert "c.parquet" not in manifest["files"]

    plan = {
        p.relpath: p
        for p in engine.build_plan(local, manifest, hasher=hashes.__getitem__)
    }
    assert plan["a.parquet"].action == engine.ACTION_SKIP
    assert plan["b.parquet"].action == engine.ACTION_UPLOAD
    assert plan["b.parquet"].reason == "changed"  # update by id, not create
    assert plan["c.parquet"].reason == "new"
    assert plan["gone.parquet"].action == engine.ACTION_ORPHAN


# --------------------------------------------------------------------------- #
# retry + keep-going
# --------------------------------------------------------------------------- #
def test_transient_upload_error_is_retried_with_backoff(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "a.parquet", b"aa")
    manifest_path = root / ".sync" / "local.json"
    attempts: list[str] = []
    waits: list[float] = []

    class Flaky(LocalBackend):
        def upload(
            self, local: Path, relpath: str, remote_id: str | None = None
        ) -> str:
            attempts.append(relpath)
            if len(attempts) < 3:
                raise TimeoutError("The read operation timed out")
            return super().upload(local, relpath, remote_id)

    backend = Flaky(tmp_path / "backup")
    lines: list[str] = []
    rc = execute_push(
        root,
        backend,
        manifest_path,
        run=True,
        attempts=5,
        backoff=2.0,
        sleep=waits.append,
        echo=lines.append,
    )
    assert rc == 0
    assert attempts == ["a.parquet"] * 3
    assert waits == [2.0, 4.0]
    assert (tmp_path / "backup" / "a.parquet").read_bytes() == b"aa"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"a.parquet"}
    assert sum("retry" in line for line in lines) == 2


def test_non_transient_error_is_not_retried(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "a.parquet", b"aa")
    calls: list[str] = []

    class Broken(LocalBackend):
        def upload(
            self, local: Path, relpath: str, remote_id: str | None = None
        ) -> str:
            calls.append(relpath)
            raise ValueError("bug, not weather")

    waits: list[float] = []
    rc = execute_push(
        root,
        Broken(tmp_path / "backup"),
        root / ".sync" / "local.json",
        run=True,
        sleep=waits.append,
        echo=lambda _line: None,
    )
    assert rc == 1 and calls == ["a.parquet"] and waits == []


def test_keep_going_is_the_default_and_fail_fast_stops(tmp_path: Path) -> None:
    root = tmp_path / "data"
    # metadata first (root-level STATUS.json), then newest first: c before b before a
    _write(root, "STATUS.json", b"{}", mtime=1_000)
    _write(root, "lane/a.parquet", b"aa", mtime=2_000)
    _write(root, "lane/b.parquet", b"bb", mtime=3_000)
    _write(root, "lane/c.parquet", b"cc", mtime=4_000)
    manifest_path = root / ".sync" / "local.json"

    class Flaky(_Recording):
        def upload(
            self, local: Path, relpath: str, remote_id: str | None = None
        ) -> str:
            if relpath == "lane/b.parquet":
                raise ValueError("permanent")
            return super().upload(local, relpath, remote_id)

    backend = Flaky(tmp_path / "backup")
    lines: list[str] = []
    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 1
    # b raises before reaching the recording base class, so it is not listed
    assert [rel for rel, _ in backend.uploads] == [
        "STATUS.json",
        "lane/c.parquet",
        "lane/a.parquet",
    ]
    assert any(
        "Push finished: 3 uploaded (6 B), 1 failed, 0 not run." in l for l in lines
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"STATUS.json", "lane/a.parquet", "lane/c.parquet"}

    # --fail-fast: stop at the failure, report the rest as not run
    manifest_path.unlink()
    for path in (tmp_path / "backup").rglob("*"):
        if path.is_file():
            path.unlink()
    backend = Flaky(tmp_path / "backup")
    lines.clear()
    rc = execute_push(
        root, backend, manifest_path, run=True, keep_going=False, echo=lines.append
    )
    assert rc == 1
    assert [rel for rel, _ in backend.uploads] == ["STATUS.json", "lane/c.parquet"]
    assert any(
        "Push finished: 2 uploaded (4 B), 1 failed, 1 not run." in l for l in lines
    )


# --------------------------------------------------------------------------- #
# plan ordering
# --------------------------------------------------------------------------- #
def test_plan_orders_metadata_first_then_newest(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "news/articles/2019-01-01.parquet", b"1", mtime=1_000)
    _write(root, "us_common/prices_daily.parquet", b"2", mtime=9_000)
    _write(root, "us_common/prices_fetch_state.csv", b"3", mtime=5_000)
    _write(root, "STATUS.json", b"4", mtime=100)
    _write(root, "_datacli_index.parquet", b"5", mtime=8_000)
    local = engine.scan_local(root)

    plan = engine.build_plan(local, {"files": {}}, hasher=lambda _r: "")
    assert [p.relpath for p in plan] == [
        "_datacli_index.parquet",  # root-level, newest of the metadata
        "us_common/prices_fetch_state.csv",  # sidecar
        "STATUS.json",  # root-level, oldest metadata
        "us_common/prices_daily.parquet",  # bulk, newest
        "news/articles/2019-01-01.parquet",  # bulk, oldest
    ]


# --------------------------------------------------------------------------- #
# reconcile command
# --------------------------------------------------------------------------- #
def test_reconcile_reports_then_rewrites_manifest_with_backup(tmp_path: Path) -> None:
    root = tmp_path / "data"
    dest = tmp_path / "backup"
    _write(root, "STATUS.json", b"{}")
    _write(root, "x.parquet", b"local")
    _write(dest, "STATUS.json", b"{}")
    _write(dest, "x.parquet", b"remote")
    _write(dest, "gone.parquet", b"only remote")
    manifest_path = root / ".sync" / "local.json"
    engine.save_manifest(manifest_path, {"version": 1, "files": {"stale": {}}})
    backend = LocalBackend(dest)
    report_path = tmp_path / "report.json"
    lines: list[str] = []

    rc = execute_reconcile(
        root,
        backend,
        manifest_path,
        run=False,
        report_path=report_path,
        echo=lines.append,
    )
    assert rc == 0
    assert any(
        "1 match local, 1 differ, 1 remote-only; 0 local-only" in l for l in lines
    )
    # dry-run: manifest untouched, report written
    assert json.loads(manifest_path.read_text(encoding="utf-8"))["files"] == {
        "stale": {}
    }
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"] == {
        "matched": 1,
        "differs": 1,
        "remote_only": 1,
        "local_only": 0,
        "duplicates": 0,
    }

    lines.clear()
    rc = execute_reconcile(root, backend, manifest_path, run=True, echo=lines.append)
    assert rc == 0
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"STATUS.json", "x.parquet", "gone.parquet"}
    backups = list(manifest_path.parent.glob("local.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text(encoding="utf-8"))["files"] == {"stale": {}}


def test_reconcile_trash_duplicates_uses_backend_trash(tmp_path: Path) -> None:
    root = tmp_path / "data"
    _write(root, "a.parquet", b"aa")
    manifest_path = root / ".sync" / "fake.json"
    trashed: list[str] = []

    class Duplicating(LocalBackend):
        def list_remote(self):
            return {
                "a.parquet": [
                    RemoteFile(
                        "keep", engine.md5_file(root / "a.parquet"), 2, "2026-08-31"
                    ),
                    RemoteFile("dup", "other", 2, "2026-09-11"),
                ]
            }

        def trash(self, remote_id: str) -> None:
            trashed.append(remote_id)

    backend = Duplicating(tmp_path / "backup")
    lines: list[str] = []
    rc = execute_reconcile(
        root,
        backend,
        manifest_path,
        run=True,
        trash_duplicates=True,
        echo=lines.append,
    )
    assert rc == 0
    assert trashed == ["dup"]  # the md5-matching copy is kept, whatever its date
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"]["a.parquet"]["remote_id"] == "keep"
