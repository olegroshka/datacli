"""Google Drive backend (``drive.file`` scope, installed-app OAuth).

No passwords are ever stored: ``sync push --run`` / ``sync login`` opens the
browser once for consent, then a refresh token is cached at
``~/.datacli/tokens/gdrive.json`` (or ``[sync] gdrive_token``). The user brings
their own OAuth client (a one-time Google Cloud Console setup — see
``storage/GDRIVE_SETUP.md``); its JSON path lives in the git-ignored
``datacli.toml``. With the ``drive.file`` scope the app can only see files it
created, so no verification review and no scary consent screen.

Resilience (after the 2026-09-11 duplicate-tree incident):

* ``upload`` without a manifest ``remote_id`` first looks the file up by name
  in its Drive folder and *updates* it. Drive happily stores two files with
  the same name in one folder, so "no id -> create" turned a lost manifest
  into a duplicated 8.7 GB tree.
* Every request runs with ``num_retries`` (googleapiclient's own 5xx/429 and
  socket-error retry, including resumable-upload chunks) over an
  ``httplib2.Http`` with an explicit socket timeout instead of the default
  "wait forever".
* ``list_remote`` walks the app's whole Drive tree in a few pages and returns
  ``{relpath: [RemoteFile...]}`` with ``md5Checksum`` so the manifest can be
  rebuilt without uploading anything. ``trash`` soft-deletes one duplicate.

Google deps are imported lazily so the core shell runs without the ``sync``
extra:  uv sync --extra sync
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any, Iterable, Mapping

from storage.backends import StorageBackend, SyncAuthError, SyncConfigError
from storage.engine import RemoteFile

SCOPES = ("https://www.googleapis.com/auth/drive.file",)
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_TOKEN_PATH = Path.home() / ".datacli" / "tokens" / "gdrive.json"
INSTALL_HINT = "Google Drive sync needs the google client libs:  uv sync --extra sync"

DEFAULT_HTTP_TIMEOUT = 120.0  # seconds per socket operation
DEFAULT_RETRIES = 5  # googleapiclient num_retries per request / upload chunk
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
LIST_FIELDS = (
    "nextPageToken, "
    "files(id, name, mimeType, md5Checksum, size, parents, modifiedTime)"
)
FILE_FIELDS = "files(id, md5Checksum, size, modifiedTime)"
_TRANSIENT_ERRNOS = frozenset(
    {errno.ECONNRESET, errno.ETIMEDOUT, errno.ECONNABORTED, errno.EPIPE}
)


def _google():
    """Import the google libs on first use, with a friendly install hint."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
        from googleapiclient.errors import HttpError
        from googleapiclient.http import MediaFileUpload
    except ImportError as exc:  # pragma: no cover - env-dependent
        raise SyncConfigError(INSTALL_HINT) from exc
    return Request, Credentials, InstalledAppFlow, build, HttpError, MediaFileUpload


def _authorized_http(creds: Any, timeout: float) -> Any:
    """An ``AuthorizedHttp`` over ``httplib2.Http(timeout=...)``, or None."""
    try:
        import httplib2
        from google_auth_httplib2 import AuthorizedHttp
    except ImportError:  # pragma: no cover - env-dependent
        return None
    http = httplib2.Http(timeout=timeout)
    # Resumable uploads answer each chunk with 308 Resume Incomplete and no
    # Location header; httplib2 would treat that as a broken redirect
    # (RedirectMissingLocation) unless redirects are left to the client lib.
    # googleapiclient.http.build_http does the same.
    http.follow_redirects = False
    return AuthorizedHttp(creds, http=http)


def _quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def remote_tree(
    files: Iterable[Mapping[str, Any]], remote_root: str
) -> dict[str, list[RemoteFile]]:
    """Turn a flat Drive listing (with ``parents``) into ``{relpath: copies}``.

    Pure so it can be tested without Drive. Paths are built by walking
    ``parents`` through the listing; a parent we cannot see (My Drive root,
    or a folder another app made) ends the walk. Only files whose full path
    starts with ``remote_root`` are kept, and folders are never returned.
    Copies for one relpath are newest-first.
    """
    by_id: dict[str, Mapping[str, Any]] = {f["id"]: f for f in files}
    path_cache: dict[str, str] = {}

    def path_of(file_id: str, visiting: frozenset[str]) -> str:
        if file_id in path_cache:
            return path_cache[file_id]
        entry = by_id[file_id]
        parent_path = ""
        for parent in entry.get("parents") or ():
            if parent in by_id and parent not in visiting:
                parent_path = path_of(parent, visiting | {file_id})
                break
        name = str(entry["name"])
        full = f"{parent_path}/{name}" if parent_path else name
        path_cache[file_id] = full
        return full

    root = remote_root.strip("/")
    prefix = f"{root}/" if root else ""
    tree: dict[str, list[RemoteFile]] = {}
    for entry in by_id.values():
        if entry.get("mimeType") == FOLDER_MIME:
            continue
        full = path_of(entry["id"], frozenset())
        if prefix and not full.startswith(prefix):
            continue
        rel = full[len(prefix) :]
        size = entry.get("size")
        tree.setdefault(rel, []).append(
            RemoteFile(
                str(entry["id"]),
                entry.get("md5Checksum"),
                int(size) if size is not None else None,
                entry.get("modifiedTime"),
            )
        )
    for copies in tree.values():
        copies.sort(key=lambda c: c.modified or "", reverse=True)
    return tree


class GDriveBackend(StorageBackend):
    """Push files into a folder tree under My Drive, mirroring relpaths."""

    name = "gdrive"

    def __init__(
        self,
        client_secrets: object = None,
        token_path: object = None,
        remote_root: str = "datacli/eodhd",
        *,
        http_timeout: float = DEFAULT_HTTP_TIMEOUT,
        retries: int = DEFAULT_RETRIES,
    ) -> None:
        self.client_secrets = Path(str(client_secrets)) if client_secrets else None
        self.token_path = Path(str(token_path)) if token_path else DEFAULT_TOKEN_PATH
        self.remote_root = remote_root.strip("/")
        self.http_timeout = float(http_timeout)
        self.retries = int(retries)
        self._service = None
        self._folder_ids: dict[str, str] = {}  # remote dir path -> Drive id

    def describe(self) -> str:
        return f"gdrive:/{self.remote_root}"

    # ------------------------------------------------------------------ auth #
    def ensure_auth(self, interactive: bool = True) -> str:
        Request, Credentials, InstalledAppFlow, build, _, _ = _google()

        creds = None
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), list(SCOPES)
                )
            except Exception:
                creds = None  # unreadable/stale cache -> fresh flow below
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                creds = None
        if creds is None or not creds.valid:
            if not interactive:
                raise SyncAuthError("not signed in to Google Drive — run: sync login")
            if self.client_secrets is None or not self.client_secrets.exists():
                raise SyncConfigError(
                    "no OAuth client configured. One-time setup (see "
                    "storage/GDRIVE_SETUP.md), then:\n"
                    "  config set sync-gdrive-secrets C:/path/to/client_secret.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(self.client_secrets), list(SCOPES)
            )
            creds = flow.run_local_server(port=0)  # opens the browser for consent
            self.token_path.parent.mkdir(parents=True, exist_ok=True)
            self.token_path.write_text(creds.to_json(), encoding="utf-8")

        http = _authorized_http(creds, self.http_timeout)
        if http is None:  # pragma: no cover - env-dependent
            self._service = build("drive", "v3", credentials=creds)
        else:
            self._service = build("drive", "v3", http=http)
        about = self._execute(self._svc().about().get(fields="user(emailAddress)"))
        email = about.get("user", {}).get("emailAddress", "?")
        return f"{email} -> Drive:/{self.remote_root}"

    def _svc(self):
        if self._service is None:
            raise SyncAuthError("backend not authenticated — call ensure_auth() first")
        return self._service

    def _execute(self, request: Any) -> Any:
        return request.execute(num_retries=self.retries)

    # --------------------------------------------------------------- folders #
    def _ensure_folder(self, dirpath: str) -> str:
        """Resolve/create the Drive folder for a remote dir path, with caching.

        ``drive.file`` scope only lists files this app created, so lookups only
        ever see our own tree — name collisions with the user's other folders
        are impossible by construction.
        """
        if dirpath in self._folder_ids:
            return self._folder_ids[dirpath]
        parent = "root"
        walked = ""
        for part in [p for p in dirpath.split("/") if p]:
            walked = f"{walked}/{part}" if walked else part
            if walked in self._folder_ids:
                parent = self._folder_ids[walked]
                continue
            query = (
                f"name = '{_quote(part)}' and '{parent}' in parents "
                f"and mimeType = '{FOLDER_MIME}' and trashed = false"
            )
            hits = self._execute(
                self._svc().files().list(q=query, fields="files(id)", pageSize=1)
            ).get("files", [])
            if hits:
                folder_id = hits[0]["id"]
            else:
                body = {"name": part, "mimeType": FOLDER_MIME, "parents": [parent]}
                folder_id = self._execute(
                    self._svc().files().create(body=body, fields="id")
                )["id"]
            self._folder_ids[walked] = folder_id
            parent = folder_id
        return parent

    def _find_files(self, name: str, folder_id: str) -> list[dict[str, Any]]:
        """Non-folder files called ``name`` directly under ``folder_id``."""
        query = (
            f"name = '{_quote(name)}' and '{folder_id}' in parents "
            f"and mimeType != '{FOLDER_MIME}' and trashed = false"
        )
        hits = self._execute(
            self._svc().files().list(q=query, fields=FILE_FIELDS, pageSize=10)
        ).get("files", [])
        return sorted(hits, key=lambda f: f.get("modifiedTime") or "", reverse=True)

    # ---------------------------------------------------------------- upload #
    def _update(self, remote_id: str, media: Any) -> str:
        return self._execute(
            self._svc().files().update(fileId=remote_id, media_body=media, fields="id")
        )["id"]

    def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
        _, _, _, _, HttpError, MediaFileUpload = _google()

        media = MediaFileUpload(str(local), resumable=True)
        if remote_id:
            try:
                return self._update(remote_id, media)
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                # remote file was deleted out from under the manifest -> fall through

        dirpath, _, name = relpath.rpartition("/")
        folder = self._ensure_folder(
            f"{self.remote_root}/{dirpath}" if dirpath else self.remote_root
        )
        # Reconcile before create: a lost manifest, or a crash between a
        # create and the manifest save, must never yield a second Drive copy.
        existing = self._find_files(name, folder)
        if existing:
            return self._update(existing[0]["id"], media)
        body = {"name": name, "parents": [folder]}
        return self._execute(
            self._svc().files().create(body=body, media_body=media, fields="id")
        )["id"]

    # ------------------------------------------------------------- reconcile #
    def list_remote(self) -> dict[str, list[RemoteFile]]:
        files: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            page = self._execute(
                self._svc()
                .files()
                .list(
                    q="trashed = false",
                    fields=LIST_FIELDS,
                    pageSize=1000,
                    pageToken=token,
                )
            )
            files.extend(page.get("files", []))
            token = page.get("nextPageToken")
            if not token:
                break
        return remote_tree(files, self.remote_root)

    def trash(self, remote_id: str) -> None:
        self._execute(
            self._svc()
            .files()
            .update(fileId=remote_id, body={"trashed": True}, fields="id")
        )

    def retryable(self, exc: BaseException) -> bool:
        if super().retryable(exc):
            return True
        status = getattr(getattr(exc, "resp", None), "status", None)
        if status in RETRY_STATUSES:
            return True  # googleapiclient HttpError
        if (type(exc).__module__ or "").startswith("httplib2"):
            return True
        if isinstance(exc, OSError) and exc.errno in _TRANSIENT_ERRNOS:
            return True
        return False
