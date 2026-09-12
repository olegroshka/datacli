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
