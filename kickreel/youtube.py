"""YouTube Data API v3 — OAuth token management and resumable clip upload."""

import time
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from . import config

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
CHUNK_SIZE = 10 * 1024 * 1024  # 10 MB per chunk


def _token_path() -> Path:
    return Path(config.YOUTUBE_TOKEN_PATH).expanduser()


def _credentials_path() -> Path:
    return Path(config.YOUTUBE_CREDENTIALS_PATH).expanduser()


def get_service():
    """Return an authenticated YouTube service, running OAuth flow if needed."""
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
                print("  YouTube token expired or revoked — re-authenticating…")
                token_path.unlink(missing_ok=True)
                creds = None
        if not creds or not creds.valid:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.parent.mkdir(parents=True, exist_ok=True)
        token_path.write_text(creds.to_json())

    return build("youtube", "v3", credentials=creds)


def thumbnail_url(video_id: str) -> str:
    return f"https://img.youtube.com/vi/{video_id}/hqdefault.jpg"


def upload_video(
    local_path: Path,
    title: str,
    description: str = "",
    privacy: str = "public",
) -> tuple[str, str]:
    """Upload a video to YouTube. Returns (video_id, watch_url)."""
    service = get_service()

    file_size = local_path.stat().st_size
    size_mb = file_size / 1e6

    print(f"  Uploading to YouTube: {local_path.name} ({size_mb:.0f} MB) …")

    body = {
        "snippet": {
            "title": title,
            "description": description,
            "categoryId": "17",  # Sports
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    media = MediaFileUpload(
        str(local_path),
        mimetype="video/mp4",
        chunksize=CHUNK_SIZE,
        resumable=True,
    )

    request = service.videos().insert(part="snippet,status", body=body, media_body=media)

    response = None
    last_pct = -1
    start_time = time.time()

    while response is None:
        try:
            status, response = request.next_chunk()
        except HttpError as e:
            if e.resp.status in (500, 502, 503, 504):
                print(f"  Transient error {e.resp.status} — retrying in 5s…")
                time.sleep(5)
                continue
            raise

        if status:
            pct = int(status.progress() * 100)
            if pct != last_pct:
                elapsed = time.time() - start_time
                uploaded_mb = file_size * status.progress() / 1e6
                speed = (uploaded_mb) / elapsed if elapsed > 0 else 0
                remaining = (file_size * (1 - status.progress()) / 1e6) / speed if speed > 0 else 0
                print(
                    f"\r    {pct:3d}%  {uploaded_mb:.0f}/{size_mb:.0f} MB"
                    f"  {speed:.1f} MB/s  ETA {int(remaining // 60)}m{int(remaining % 60):02d}s  ",
                    end="",
                    flush=True,
                )
                last_pct = pct

    print(f"\r  ✓ {local_path.name}                                              ")

    video_id = response["id"]
    return video_id, f"https://www.youtube.com/watch?v={video_id}"
