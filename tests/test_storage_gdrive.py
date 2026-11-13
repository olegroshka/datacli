"""Drive backend behaviour against a fake ``files()`` service (no network)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

pytest.importorskip("googleapiclient")

from storage import gdrive  # noqa: E402
from storage.engine import RemoteFile  # noqa: E402

FOLDER = gdrive.FOLDER_MIME


class _Request:
    def __init__(self, result):
        self._result = result

    def execute(self, num_retries=0):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Files:
    """Records calls; answers list() from a canned table keyed by query."""

    def __init__(self, listings: dict[str, list[dict]]):
        self.listings = listings
        self.calls: list[tuple[str, dict]] = []
        self.next_id = 100

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        query = kwargs.get("q", "")
        for needle, files in self.listings.items():
            if needle in query:
                return _Request({"files": files})
        return _Request({"files": []})

    def create(self, **kwargs):
        self.calls.append(("create", kwargs))
        self.next_id += 1
        return _Request({"id": f"new-{self.next_id}"})

    def update(self, **kwargs):
        self.calls.append(("update", kwargs))
        return _Request({"id": kwargs["fileId"]})


class _Service:
    def __init__(self, files: _Files):
        self._files = files

    def files(self):
        return self._files


def _backend(files: _Files) -> gdrive.GDriveBackend:
    backend = gdrive.GDriveBackend(remote_root="datacli/eodhd", retries=2)
    backend._service = _Service(files)
    return backend


def test_upload_without_remote_id_updates_existing_copy_instead_of_creating(
    tmp_path: Path,
) -> None:
    local = tmp_path / "STATUS.json"
    local.write_text("{}", encoding="utf-8")
    files = _Files(
        {
            "name = 'datacli' and 'root' in parents": [{"id": "f-datacli"}],
            "name = 'eodhd' and 'f-datacli' in parents": [{"id": "f-eodhd"}],
            "name = 'STATUS.json' and 'f-eodhd' in parents": [
                {"id": "old", "modifiedTime": "2026-08-31T10:00:00Z"},
                {"id": "newer", "modifiedTime": "2026-09-11T09:35:00Z"},
            ],
        }
    )
    backend = _backend(files)

    assert backend.upload(local, "STATUS.json", remote_id=None) == "newer"
    kinds = [kind for kind, _ in files.calls]
    assert "create" not in kinds
    update = next(kw for kind, kw in files.calls if kind == "update")
    assert update["fileId"] == "newer"  # newest copy wins when no md5 is known


def test_upload_creates_only_when_folder_has_no_copy(tmp_path: Path) -> None:
    local = tmp_path / "x.parquet"
    local.write_bytes(b"pq")
    files = _Files(
        {
            "name = 'datacli' and 'root' in parents": [{"id": "f-datacli"}],
            "name = 'eodhd' and 'f-datacli' in parents": [{"id": "f-eodhd"}],
            "name = 'news' and 'f-eodhd' in parents": [{"id": "f-news"}],
        }
    )
    backend = _backend(files)

    remote_id = backend.upload(local, "news/x.parquet", remote_id=None)
    assert remote_id.startswith("new-")
    create = next(kw for kind, kw in files.calls if kind == "create")
    assert create["body"] == {"name": "x.parquet", "parents": ["f-news"]}


def test_upload_with_remote_id_updates_in_place_without_lookup(tmp_path: Path) -> None:
    local = tmp_path / "x.parquet"
    local.write_bytes(b"pq")
    files = _Files({})
    backend = _backend(files)

    assert backend.upload(local, "news/x.parquet", remote_id="known") == "known"
    assert [kind for kind, _ in files.calls] == ["update"]


def test_remote_tree_builds_relpaths_under_root_and_groups_duplicates() -> None:
    listing = [
        {"id": "f-datacli", "name": "datacli", "mimeType": FOLDER, "parents": ["root"]},
        {
            "id": "f-eodhd",
            "name": "eodhd",
            "mimeType": FOLDER,
            "parents": ["f-datacli"],
        },
        {
            "id": "f-idx",
            "name": "index_ref",
            "mimeType": FOLDER,
            "parents": ["f-eodhd"],
        },
        {
            "id": "s-old",
            "name": "STATUS.json",
            "parents": ["f-eodhd"],
            "md5Checksum": "aaa",
            "size": "10",
            "modifiedTime": "2026-08-20T00:00:00Z",
        },
        {
            "id": "s-new",
            "name": "STATUS.json",
            "parents": ["f-eodhd"],
            "md5Checksum": "bbb",
            "size": "11",
            "modifiedTime": "2026-09-11T00:00:00Z",
        },
        {
            "id": "p1",
            "name": "prices_daily.parquet",
            "parents": ["f-idx"],
            "md5Checksum": "ccc",
            "size": "5",
            "modifiedTime": "2026-08-31T00:00:00Z",
        },
        # an older experiment elsewhere in My Drive: not under the remote root
        {
            "id": "f-other",
            "name": "experiments",
            "mimeType": FOLDER,
            "parents": ["root"],
        },
        {"id": "p-other", "name": "prices_daily.parquet", "parents": ["f-other"]},
    ]

    tree = gdrive.remote_tree(listing, "datacli/eodhd")

    assert set(tree) == {"STATUS.json", "index_ref/prices_daily.parquet"}
    assert [c.remote_id for c in tree["STATUS.json"]] == ["s-new", "s-old"]
    assert tree["index_ref/prices_daily.parquet"] == [
        RemoteFile("p1", "ccc", 5, "2026-08-31T00:00:00Z")
    ]


def test_list_remote_pages_through_the_listing() -> None:
    page1 = {
        "files": [
            {
                "id": "f-datacli",
                "name": "datacli",
                "mimeType": FOLDER,
                "parents": ["root"],
            },
            {
                "id": "f-eodhd",
                "name": "eodhd",
                "mimeType": FOLDER,
                "parents": ["f-datacli"],
            },
        ],
        "nextPageToken": "t2",
    }
    page2 = {
        "files": [
            {"id": "a", "name": "STATUS.md", "parents": ["f-eodhd"], "md5Checksum": "m"}
        ]
    }

    class _Paged(_Files):
        def list(self, **kwargs):
            self.calls.append(("list", kwargs))
            return _Request(page2 if kwargs.get("pageToken") == "t2" else page1)

    files = _Paged({})
    backend = _backend(files)
    tree = backend.list_remote()
    assert set(tree) == {"STATUS.md"}
    assert tree["STATUS.md"][0].remote_id == "a"
    assert [kw.get("pageToken") for _, kw in files.calls] == [None, "t2"]


def test_trash_marks_file_trashed_not_deleted() -> None:
    files = _Files({})
    backend = _backend(files)
    backend.trash("dup")
    kind, kwargs = files.calls[-1]
    assert kind == "update" and kwargs["fileId"] == "dup"
    assert kwargs["body"] == {"trashed": True}


def test_retryable_classifies_transport_and_server_errors() -> None:
    import httplib2
    from googleapiclient.errors import HttpError

    backend = gdrive.GDriveBackend()
    assert backend.retryable(TimeoutError("The read operation timed out"))
    assert backend.retryable(ConnectionResetError(10054, "reset"))
    assert backend.retryable(HttpError(httplib2.Response({"status": 503}), b""))
    assert backend.retryable(HttpError(httplib2.Response({"status": 429}), b""))
    assert not backend.retryable(HttpError(httplib2.Response({"status": 403}), b""))
    assert not backend.retryable(FileNotFoundError("gone"))
    assert not backend.retryable(ValueError("bug"))


def test_every_request_carries_num_retries() -> None:
    seen: list[int] = []

    class _Counting(_Request):
        def execute(self, num_retries=0):
            seen.append(num_retries)
            return super().execute(num_retries)

    class _CountingFiles(_Files):
        def update(self, **kwargs):
            self.calls.append(("update", kwargs))
            return _Counting({"id": kwargs["fileId"]})

    backend = _backend(_CountingFiles({}))
    backend.trash("x")
    assert seen == [2]


def test_authorized_http_has_timeout_and_does_not_follow_redirects() -> None:
    """Run 20260912T040001.739801Z-96e97d4d6f01: every file larger than one
    resumable chunk failed with RedirectMissingLocation because httplib2 tried
    to follow the 308 Resume Incomplete that carries no Location header."""

    class _Creds:
        token = "t"
        expired = False
        valid = True

        def before_request(self, request, method, url, headers):  # pragma: no cover
            headers["authorization"] = "Bearer t"

    authorized = gdrive._authorized_http(_Creds(), 120.0)
    assert authorized is not None
    assert authorized.http.timeout == 120.0
    assert authorized.http.follow_redirects is False


def test_each_thread_gets_its_own_service(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    backend = gdrive.GDriveBackend(remote_root="datacli/eodhd")
    backend._creds = object()
    built: list[object] = []

    def fake_build(creds):
        svc = _Service(_Files({}))
        built.append(svc)
        return svc

    monkeypatch.setattr(backend, "_build_service", fake_build)
    main = backend._svc()
    assert backend._svc() is main
    seen: list[object] = []
    threads = [
        threading.Thread(target=lambda: seen.append(backend._svc())) for _ in range(3)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(s) for s in seen + [main]}) == 4 and len(built) == 4


def test_parallel_folder_resolution_creates_each_folder_once(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    files = _Files({})
    backend = _backend(files)
    with ThreadPoolExecutor(8) as pool:
        ids = set(
            pool.map(
                lambda _: backend._ensure_folder("datacli/eodhd/intraday/bars_1m"),
                range(16),
            )
        )
    assert len(ids) == 1
    creates = [kw["body"]["name"] for kind, kw in files.calls if kind == "create"]
    assert creates == ["datacli", "eodhd", "intraday", "bars_1m"]


def test_drive_rate_limit_403_is_retryable() -> None:
    class _Resp:
        status = 403

    class RateLimited(Exception):
        resp = _Resp()
        content = b'{"error": {"errors": [{"reason": "userRateLimitExceeded"}]}}'

    class Forbidden(Exception):
        resp = _Resp()
        content = b'{"error": {"errors": [{"reason": "insufficientPermissions"}]}}'

    backend = gdrive.GDriveBackend()
    assert backend.retryable(RateLimited())
    assert not backend.retryable(Forbidden())


def test_workers_setting_overrides_the_default() -> None:
    from storage.backends import make_backend

    assert make_backend({"backend": "gdrive"}).max_workers == gdrive.DEFAULT_WORKERS
    assert make_backend({"backend": "gdrive", "push_workers": 3}).max_workers == 3


# --------------------------------------------------------------------------- #
# DD-001: one listing per unit, worker pool, shared pause, single refresh
# --------------------------------------------------------------------------- #
def _push_fixture(tmp_path: Path, listing: list[dict]):
    import json
    import os

    from storage import engine
    from storage.cli import execute_push

    pytest.importorskip("rich")
    root = tmp_path / "data"
    for rel in (
        "STATUS.json",
        "a/x.parquet",
        "a/y.parquet",
        "b/z.parquet",
        "old.parquet",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(rel.encode())
        os.utime(path, (1_000, 1_000))
    manifest_path = root / ".sync" / "gdrive.json"
    old = root / "old.parquet"
    manifest: dict = {"files": {}}
    engine.record_push(
        manifest,
        engine.FileStat("old.parquet", old.stat().st_size, old.stat().st_mtime_ns),
        md5="m-old",
        remote_id="r-old",
    )
    engine.save_manifest(manifest_path, manifest)

    class _Listing(_Files):
        def list(self, **kwargs):
            if kwargs.get("q") == "trashed = false":
                self.calls.append(("listing", kwargs))
                return _Request({"files": listing})
            return super().list(**kwargs)

    files = _Listing({})
    backend = _backend(files)
    backend.ensure_auth = lambda interactive=True: "fake -> Drive:/datacli/eodhd"
    lines: list[str] = []
    rc = execute_push(root, backend, manifest_path, run=True, echo=lines.append)
    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
    return rc, files, backend, saved, lines, (root, manifest_path, execute_push)


def test_push_lists_once_and_creates_new_files_without_lookups(tmp_path: Path) -> None:
    rc, files, backend, saved, lines, (root, manifest_path, execute_push) = (
        _push_fixture(tmp_path, [])
    )
    assert rc == 0
    kinds = [kind for kind, _ in files.calls]
    assert kinds.count("listing") == 1
    lookups = [kw["q"] for kind, kw in files.calls if kind == "list"]
    assert all(f"mimeType = '{FOLDER}'" in q for q in lookups)  # folders only
    assert len(lookups) == 4  # datacli, eodhd, a, b
    creates = [kw["body"]["name"] for kind, kw in files.calls if kind == "create"]
    assert sorted(creates) == sorted(
        [
            "datacli",
            "eodhd",
            "a",
            "b",
            "STATUS.json",
            "x.parquet",
            "y.parquet",
            "z.parquet",
        ]
    )
    assert set(saved["files"]) == {
        "STATUS.json",
        "a/x.parquet",
        "a/y.parquet",
        "b/z.parquet",
        "old.parquet",
    }
    files.calls.clear()
    assert execute_push(root, backend, manifest_path, run=True, echo=lines.append) == 0
    assert files.calls == []  # a repeat push issues nothing


def test_push_updates_a_file_the_listing_holds_instead_of_creating(
    tmp_path: Path,
) -> None:
    listing = [
        {"id": "f-datacli", "name": "datacli", "mimeType": FOLDER, "parents": ["root"]},
        {
            "id": "f-eodhd",
            "name": "eodhd",
            "mimeType": FOLDER,
            "parents": ["f-datacli"],
        },
        {"id": "f-a", "name": "a", "mimeType": FOLDER, "parents": ["f-eodhd"]},
        {
            "id": "r-x",
            "name": "x.parquet",
            "parents": ["f-a"],
            "modifiedTime": "2026-11-13T10:00:00Z",
        },
    ]
    rc, files, _backend_, saved, _lines, _ = _push_fixture(tmp_path, listing)
    assert rc == 0
    updates = [kw["fileId"] for kind, kw in files.calls if kind == "update"]
    assert updates == ["r-x"]
    creates = [kw["body"]["name"] for kind, kw in files.calls if kind == "create"]
    assert "x.parquet" not in creates
    assert saved["files"]["a/x.parquet"]["remote_id"] == "r-x"


def test_a_retry_after_a_listed_create_looks_the_path_up(tmp_path: Path) -> None:
    """A create whose answer was lost must not become a second copy (DD-001 S2)."""
    from storage.cli import _upload_with_retry

    local = tmp_path / "x.parquet"
    local.write_bytes(b"x")

    class _LostAnswer(_Files):
        def create(self, **kwargs):
            if "media_body" in kwargs and not getattr(self, "lost", False):
                self.lost = True
                self.calls.append(("create", kwargs))
                raise ConnectionResetError(104, "reset")  # created, answer lost
            return super().create(**kwargs)

    files = _LostAnswer(
        {
            "name = 'datacli' and 'root' in parents": [{"id": "f-datacli"}],
            "name = 'eodhd' and 'f-datacli' in parents": [{"id": "f-eodhd"}],
            "name = 'x.parquet' and 'f-eodhd' in parents": [{"id": "r-x"}],
        }
    )
    backend = _backend(files)
    rid = _upload_with_retry(
        backend,
        local,
        "x.parquet",
        None,
        known_absent=True,
        attempts=3,
        backoff=0,
        max_backoff=0,
        sleep=lambda _s: None,
        echo=lambda _l: None,
    )
    assert rid == "r-x"
    assert [k for k, _ in files.calls].count("create") == 1  # the lost one only


def test_a_rate_limit_pauses_every_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status = 429

    class TooMany(Exception):
        resp = _Resp()
        content = b""

    backend = _backend(_Files({}))
    slept: list[float] = []
    clock = [100.0]
    monkeypatch.setattr(gdrive.time, "monotonic", lambda: clock[0])

    def fake_sleep(s: float) -> None:
        slept.append(s)
        clock[0] += s

    monkeypatch.setattr(gdrive.time, "sleep", fake_sleep)
    with pytest.raises(TooMany):
        backend._execute(_Request(TooMany()))
    assert backend._pause_until == 100.0 + backend.rate_limit_pause
    # the next request on any thread waits out the pause first
    assert backend._execute(_Request({"ok": 1})) == {"ok": 1}
    assert slept == [backend.rate_limit_pause]


def test_credentials_refresh_once_across_workers() -> None:
    import threading
    import time as _time

    class _Creds:
        def __init__(self) -> None:
            self.token = "t0"
            self.valid = False
            self.refreshes = 0

        def refresh(self, request) -> None:
            _time.sleep(0.05)
            self.refreshes += 1
            self.token = f"t{self.refreshes}"
            self.valid = True

    creds = gdrive._refresh_once(_Creds())
    threads = [threading.Thread(target=creds.refresh, args=(None,)) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert creds.refreshes == 1 and creds.token == "t1"
    creds.valid = False  # expires again later: refreshes again
    creds.refresh(None)
    assert creds.refreshes == 2
