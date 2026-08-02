from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from storage import engine  # noqa: E402
from storage.backends import LocalBackend, SyncConfigError, make_backend  # noqa: E402


def _write(root: Path, relpath: str, content: bytes) -> Path:
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


# --------------------------------------------------------------------------- #
# scan
# --------------------------------------------------------------------------- #
def test_scan_includes_datasets_and_skips_caches(tmp_path: Path) -> None:
    _write(tmp_path, "us_common/prices.parquet", b"pq")
    _write(tmp_path, "us_common/prices_fetch_state.csv", b"csv")
    _write(tmp_path, "STATUS.md", b"md")
    _write(tmp_path, "STATUS.json", b"{}")
    # excluded: payload caches at any depth, the manifest dir, non-matching exts
    _write(tmp_path, "us_common/cache/AAPL.json.gz", b"gz")
    _write(tmp_path, "probe_cache/US/x.json", b"{}")
    _write(tmp_path, ".sync/gdrive.json", b"{}")
    _write(tmp_path, "notes.txt", b"txt")

    found = engine.scan_local(tmp_path)
    assert set(found) == {
        "us_common/prices.parquet",
        "us_common/prices_fetch_state.csv",
        "STATUS.md",
        "STATUS.json",
    }
    stat = found["us_common/prices.parquet"]
    assert stat.size == 2
    assert "\\" not in stat.relpath  # POSIX relpaths -> portable manifests


# --------------------------------------------------------------------------- #
# plan
# --------------------------------------------------------------------------- #
def test_build_plan_actions(tmp_path: Path) -> None:
    _write(tmp_path, "a.parquet", b"aaa")
    _write(tmp_path, "b.parquet", b"bbb")
    _write(tmp_path, "c.parquet", b"ccc")
    local = engine.scan_local(tmp_path)

    manifest = {
        "files": {
            # b unchanged: size+mtime match -> skip, hasher never called
            "b.parquet": {
                "size": local["b.parquet"].size,
                "mtime_ns": local["b.parquet"].mtime_ns,
                "md5": "irrelevant",
            },
            # c changed: mtime differs, content hash differs -> upload
            "c.parquet": {"size": 3, "mtime_ns": 1, "md5": "old-hash"},
            # d gone locally -> orphan
            "d.parquet": {"size": 9, "mtime_ns": 1, "md5": "x"},
        }
    }
    hashed: list[str] = []

    def hasher(relpath: str) -> str:
        hashed.append(relpath)
        return engine.md5_file(tmp_path / relpath)

    plan = {p.relpath: p for p in engine.build_plan(local, manifest, hasher=hasher)}
    assert plan["a.parquet"].action == engine.ACTION_UPLOAD  # new
    assert plan["b.parquet"].action == engine.ACTION_SKIP
    assert plan["c.parquet"].action == engine.ACTION_UPLOAD  # changed
    assert plan["d.parquet"].action == engine.ACTION_ORPHAN
    assert hashed == ["c.parquet"]  # only the size/mtime mismatch got hashed

    summary = engine.summarize_plan(plan.values())
    assert summary["upload"] == 2
    assert summary["upload_bytes"] == 6
    assert summary["orphan"] == 1


def test_build_plan_touch_when_content_identical(tmp_path: Path) -> None:
    path = _write(tmp_path, "a.parquet", b"same")
    local = engine.scan_local(tmp_path)
    manifest = {
        "files": {
            "a.parquet": {
                "size": 4,
                "mtime_ns": local["a.parquet"].mtime_ns - 1,  # mtime moved
                "md5": engine.md5_file(path),  # ...but content did not
            }
        }
    }
    (item,) = engine.build_plan(
        local, manifest, hasher=lambda rel: engine.md5_file(tmp_path / rel)
    )
    assert item.action == engine.ACTION_TOUCH
    assert item.md5 == engine.md5_file(path)


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def test_manifest_round_trip_and_bad_file(tmp_path: Path) -> None:
    path = tmp_path / ".sync" / "gdrive.json"
    manifest = engine.load_manifest(path)
    assert manifest["files"] == {}

    stat = engine.FileStat("a.parquet", 3, 123)
    engine.record_push(manifest, stat, md5="abc", remote_id="id-1")
    engine.save_manifest(path, manifest)

    loaded = engine.load_manifest(path)
    entry = loaded["files"]["a.parquet"]
    assert entry["md5"] == "abc"
    assert entry["remote_id"] == "id-1"
    assert entry["uploaded_at"].endswith("Z")

    path.write_text("not json", encoding="utf-8")
    assert engine.load_manifest(path)["files"] == {}  # corrupt -> fresh, not a crash


# --------------------------------------------------------------------------- #
# backends
# --------------------------------------------------------------------------- #
def test_make_backend_local_and_errors(tmp_path: Path) -> None:
    backend = make_backend({"backend": "local", "local_dest": str(tmp_path / "dst")})
    assert isinstance(backend, LocalBackend)
    with pytest.raises(SyncConfigError):
        make_backend({"backend": "local"})  # local_dest missing
    with pytest.raises(SyncConfigError):
        make_backend({"backend": "dropbox"})  # unknown


def test_local_backend_push_end_to_end(tmp_path: Path, capsys) -> None:
    pytest.importorskip("rich")
    from storage.cli import execute_push

    root = tmp_path / "data"
    _write(root, "us_common/prices.parquet", b"v1-content")
    _write(root, "STATUS.md", b"status")
    dest = tmp_path / "backup"
    manifest_path = root / ".sync" / "local.json"
    backend = LocalBackend(dest)

    # dry-run: plans but copies nothing
    assert execute_push(root, backend, manifest_path, run=False) == 0
    assert not dest.exists() or not any(dest.rglob("*"))

    # real run: copies both files and records them in the manifest
    assert execute_push(root, backend, manifest_path, run=True) == 0
    assert (dest / "us_common/prices.parquet").read_bytes() == b"v1-content"
    assert (dest / "STATUS.md").read_bytes() == b"status"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"us_common/prices.parquet", "STATUS.md"}

    # unchanged re-run: nothing to do
    assert execute_push(root, backend, manifest_path, run=True) == 0
    assert "Everything in sync." in capsys.readouterr().out

    # change a file -> only that file is re-pushed
    _write(root, "us_common/prices.parquet", b"v2-content!")
    assert execute_push(root, backend, manifest_path, run=True) == 0
    assert (dest / "us_common/prices.parquet").read_bytes() == b"v2-content!"


def test_push_stops_on_failure_but_keeps_progress(tmp_path: Path) -> None:
    pytest.importorskip("rich")
    from storage.cli import execute_push

    root = tmp_path / "data"
    _write(root, "a.parquet", b"aa")
    _write(root, "b.parquet", b"bb")
    manifest_path = root / ".sync" / "local.json"

    class FlakyBackend(LocalBackend):
        def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
            if relpath == "b.parquet":
                raise OSError("disk full")
            return super().upload(local, relpath, remote_id)

    backend = FlakyBackend(tmp_path / "backup")
    assert execute_push(root, backend, manifest_path, run=True) == 1
    # a succeeded and was recorded before b failed -> resume skips a
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert set(manifest["files"]) == {"a.parquet"}
