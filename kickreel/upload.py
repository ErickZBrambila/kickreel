from pathlib import Path

from .youtube import upload_video


def upload_clip(local_path: Path, title: str, description: str = "") -> tuple[str, str]:
    """Upload a clip to YouTube. Returns (video_id, watch_url)."""
    return upload_video(local_path, title, description)
