from pathlib import Path

import boto3

from . import config
from .youtube import upload_video


def upload_clip(local_path: Path, title: str, description: str = "") -> tuple[str, str]:
    """Upload a clip to YouTube. Returns (video_id, watch_url)."""
    return upload_video(local_path, title, description)


def upload_file(local_path: Path, r2_key: str, content_type: str = "video/mp4") -> str:
    """Upload a file to R2 (download storage). Returns public URL."""
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )
    client.upload_file(
        str(local_path),
        config.R2_BUCKET,
        r2_key,
        ExtraArgs={
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000",
        },
    )
    return f"{config.R2_PUBLIC_URL}/{r2_key}"
