"""Google Drive integration — OAuth token management and resumable file upload."""

import os
import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from . import config

SCOPES = ["https://www.googleapis.com/auth/drive.file"]
CHUNK_SIZE = 50 * 1024 * 1024  # 50 MB per chunk


def _token_path() -> Path:
    return Path(config.DRIVE_TOKEN_PATH).expanduser()


def _credentials_path() -> Path:
    return Path(config.DRIVE_CREDENTIALS_PATH).expanduser()


def get_service():
    """Return an authenticated Drive service, running OAuth flow if needed."""
    creds = None
    token_path = _token_path()
    creds_path = _credentials_path()

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Google OAuth credentials not found at {creds_path}\n"
            "Download them from Google Cloud Console:\n"
            "  APIs & Services → Credentials → Create OAuth 2.0 Client ID\n"
            "  Application type: Desktop app → Download JSON → save to that path"
        )

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Refresh token revoked or expired — delete token and re-run OAuth flow
                print("  Drive token expired or revoked — re-authenticating…")
                token_path.unlink(missing_ok=True)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds)


def ensure_folder(service, name: str, parent_id: str | None = None) -> str:
    """Return the Drive folder ID for `name`, creating it if it doesn't exist."""
    query_parts = [
        "mimeType='application/vnd.google-apps.folder'",
        f"name='{name}'",
        "trashed=false",
    ]
    if parent_id:
        query_parts.append(f"'{parent_id}' in parents")

    results = service.files().list(
        q=" and ".join(query_parts),
        spaces="drive",
        fields="files(id, name)",
    ).execute()

    files = results.get("files", [])
    if files:
        return files[0]["id"]

    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]

    folder = service.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def upload_video(
    service,
    local_path: Path,
    filename: str,
    folder_id: str,
    make_shareable: bool = True,
) -> tuple[str, str]:
    """
    Upload local_path to Drive with a resumable upload.
    Returns (file_id, share_url).
    """
    file_size = local_path.stat().st_size
    size_gb = file_size / 1e9

    print(f"  Uploading {filename} ({size_gb:.1f} GB) …")

    media = MediaFileUpload(
        str(local_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    metadata = {
        "name": filename,
        "parents": [folder_id],
    }

    request = service.files().create(body=metadata, media_body=media, fields="id")

    response = None
    last_pct = -1
    start_time = time.time()

    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                print(f"  Transient error {e.resp.status} — retrying in 5s …")
                time.sleep(5)
                continue
            raise

        if status:
            pct = int(status.progress() * 100)
            if pct != last_pct:
                elapsed = time.time() - start_time
                uploaded_gb = file_size * status.progress() / 1e9
                speed_mbps = (uploaded_gb * 1024) / elapsed if elapsed > 0 else 0
                remaining = (file_size * (1 - status.progress()) / 1e9 * 1024) / speed_mbps if speed_mbps > 0 else 0
                print(
                    f"\r  {pct:3d}%  {uploaded_gb:.1f}/{size_gb:.1f} GB"
                    f"  {speed_mbps:.1f} MB/s  ETA {int(remaining // 60)}m{int(remaining % 60):02d}s  ",
                    end="",
                    flush=True,
                )
                last_pct = pct

    print(f"\r  ✓ Uploaded {filename}                                        ")

    file_id = response["id"]

    if make_shareable:
        service.permissions().create(
            fileId=file_id,
            body={"type": "anyone", "role": "reader"},
        ).execute()

    share_url = f"https://drive.google.com/file/d/{file_id}/view"
    return file_id, share_url
