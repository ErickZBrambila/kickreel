from pathlib import Path

import boto3

from . import config


def _client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=config.R2_SECRET_ACCESS_KEY,
        region_name="auto",
    )


def upload_file(local_path: Path, r2_key: str, content_type: str = "video/mp4") -> str:
    """Upload local_path to R2 at r2_key, return public URL."""
    _client().upload_file(
        str(local_path),
        config.R2_BUCKET,
        r2_key,
        ExtraArgs={
            "ContentType": content_type,
            "CacheControl": "public, max-age=31536000",
        },
    )
    return f"{config.R2_PUBLIC_URL}/{r2_key}"
