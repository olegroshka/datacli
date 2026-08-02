"""Google Drive backend (``drive.file`` scope, installed-app OAuth).

No passwords are ever stored: ``sync push --run`` / ``sync login`` opens the
browser once for consent, then a refresh token is cached at
``~/.datacli/tokens/gdrive.json`` (or ``[sync] gdrive_token``). The user brings
their own OAuth client (a one-time Google Cloud Console setup — see
``storage/GDRIVE_SETUP.md``); its JSON path lives in the git-ignored
``datacli.toml``. With the ``drive.file`` scope the app can only see files it
created, so no verification review and no scary consent screen.

Google deps are imported lazily so the core shell runs without the ``sync``
extra:  uv sync --extra sync
"""

from __future__ import annotations

from pathlib import Path

from storage.backends import StorageBackend, SyncAuthError, SyncConfigError

SCOPES = ("https://www.googleapis.com/auth/drive.file",)
FOLDER_MIME = "application/vnd.google-apps.folder"
DEFAULT_TOKEN_PATH = Path.home() / ".datacli" / "tokens" / "gdrive.json"
INSTALL_HINT = "Google Drive sync needs the google client libs:  uv sync --extra sync"


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


class GDriveBackend(StorageBackend):
    """Push files into a folder tree under My Drive, mirroring relpaths."""

    name = "gdrive"

    def __init__(
        self,
        client_secrets: object = None,
        token_path: object = None,
        remote_root: str = "datacli/eodhd",
    ) -> None:
        self.client_secrets = Path(str(client_secrets)) if client_secrets else None
        self.token_path = (
            Path(str(token_path)) if token_path else DEFAULT_TOKEN_PATH
        )
        self.remote_root = remote_root.strip("/")
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

        self._service = build("drive", "v3", credentials=creds)
        about = self._service.about().get(fields="user(emailAddress)").execute()
        email = about.get("user", {}).get("emailAddress", "?")
        return f"{email} -> Drive:/{self.remote_root}"

    def _svc(self):
        if self._service is None:
            raise SyncAuthError("backend not authenticated — call ensure_auth() first")
        return self._service

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
            safe = part.replace("'", "\\'")
            query = (
                f"name = '{safe}' and '{parent}' in parents "
                f"and mimeType = '{FOLDER_MIME}' and trashed = false"
            )
            hits = (
                self._svc()
                .files()
                .list(q=query, fields="files(id)", pageSize=1)
                .execute()
                .get("files", [])
            )
            if hits:
                folder_id = hits[0]["id"]
            else:
                body = {"name": part, "mimeType": FOLDER_MIME, "parents": [parent]}
                folder_id = (
                    self._svc().files().create(body=body, fields="id").execute()["id"]
                )
            self._folder_ids[walked] = folder_id
            parent = folder_id
        return parent

    # ---------------------------------------------------------------- upload #
    def upload(self, local: Path, relpath: str, remote_id: str | None = None) -> str:
        _, _, _, _, HttpError, MediaFileUpload = _google()

        media = MediaFileUpload(str(local), resumable=True)
        if remote_id:
            try:
                return (
                    self._svc()
                    .files()
                    .update(fileId=remote_id, media_body=media, fields="id")
                    .execute()["id"]
                )
            except HttpError as exc:
                if exc.resp.status != 404:
                    raise
                # remote file was deleted out from under the manifest -> recreate

        dirpath, _, name = relpath.rpartition("/")
        folder = self._ensure_folder(
            f"{self.remote_root}/{dirpath}" if dirpath else self.remote_root
        )
        body = {"name": name, "parents": [folder]}
        return (
            self._svc()
            .files()
            .create(body=body, media_body=media, fields="id")
            .execute()["id"]
        )
