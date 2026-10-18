"""Step 4 — Upload full game videos to Google Drive and patch Sanity with share URL."""

import requests
from pathlib import Path

from .drive import get_service, ensure_folder, upload_video
from .manifest import load, save
from . import config


def _patch_sanity_drive_url(doc_id: str, video_index: int, drive_url: str) -> None:
    r = requests.post(
        f"https://{config.SANITY_PROJECT_ID}.api.sanity.io/v2024-01-01/data/mutate/{config.SANITY_DATASET}",
        headers={"Authorization": f"Bearer {config.SANITY_WRITE_TOKEN}", "Content-Type": "application/json"},
        json={"mutations": [{"patch": {
            "id": doc_id,
            "set": {f"videos[{video_index}].driveUrl": drive_url},
        }}]},
    )
    if r.status_code != 200:
        print(f"  Warning: Sanity patch failed: {r.status_code} {r.text}")


def run(library: Path, dry_run: bool = False, match_id: str | None = None) -> None:
    print(f"\nKickReel drive-upload{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
    )

    if not session_dirs:
        print("No sessions found.")
        return

    service = None if dry_run else get_service()

    root_folder_id = None if dry_run else ensure_folder(service, config.DRIVE_ROOT_FOLDER)

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        has_drive = any(v.drive_url for v in manifest.videos if v.drive_url)
        if has_drive:
            print(f"[skip] {manifest.session_name} — already on Drive")
            continue

        if not manifest.offloaded:
            print(f"[skip] {manifest.session_name} — not yet offloaded")
            continue

        print(f"{'─' * 50}")
        print(f"Session : {manifest.session_name}  [{manifest.session_type}]")

        date_slug = manifest.session_date[:10] if manifest.session_date else "unknown"
        folder_name = f"{date_slug} {manifest.session_name}"

        session_folder_id = None
        if not dry_run:
            session_folder_id = ensure_folder(service, folder_name, parent_id=root_folder_id)

        for idx, video in enumerate(manifest.videos):
            video_path = Path(video.local_path)
            if not video_path.exists():
                print(f"  [missing] {video.file_name} — file not found at {video_path}")
                continue

            size_gb = video_path.stat().st_size / 1e9

            if dry_run:
                print(f"  would upload: {video.file_name} ({size_gb:.1f} GB) → Drive/{folder_name}/")
                continue

            file_id, share_url = upload_video(
                service, video_path, video.file_name, session_folder_id,
            )

            video.drive_file_id = file_id
            video.drive_url = share_url
            print(f"  Share URL: {share_url}")

            # Patch Sanity so the website shows the link immediately
            if manifest.sanity_doc_id:
                _patch_sanity_drive_url(manifest.sanity_doc_id, idx, share_url)
                print(f"  ✓ Sanity updated")

        if not dry_run:
            save(session_dir, manifest)
            print(f"  ✓ Manifest updated")

    print(f"\n{'─' * 50}")
    print("Drive upload complete.\n")
