"""Step 3 — Upload clips to R2 and publish metadata to Sanity."""

from pathlib import Path

from . import config
from .manifest import load, save
from .publish import PublishedVideo, create_session_document
from .upload import upload_file


def run(library: Path, team: str, dry_run: bool = False) -> None:
    print(f"\nKickReel publish{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    team_id = config.SANITY_TEAM_U11_ID if team == "u11" else config.SANITY_TEAM_U12_ID

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
    )

    if not session_dirs:
        print("No sessions found. Run 'offload' and 'process' first.")
        return

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        if manifest.published:
            print(f"[skip] {manifest.session_name} — already published")
            continue

        if not manifest.processed:
            print(f"[skip] {manifest.session_name} — not yet processed")
            continue

        has_analytics = any(v.analytics for v in manifest.videos if v.track_path)
        if not has_analytics and any(v.track_path for v in manifest.videos):
            print(f"  Warning: no analytics found — run 'analyze' before 'publish' for heatmap data")

        print(f"{'─'*50}")
        print(f"Session : {manifest.session_name}  [{manifest.session_type}]")

        date_slug = manifest.session_date[:10].replace("-", "") if manifest.session_date else "unknown"
        published_videos: list[PublishedVideo] = []

        for video in manifest.videos:
            if not video.clips:
                continue

            print(f"\n  [{video.file_name}]")

            clips_dir = session_dir / "clips"

            if dry_run:
                thumb_exists = video.cover_path and Path(video.cover_path).exists()
                print(f"  thumbnail : {'found' if thumb_exists else 'missing'}")
                for clip in video.clips:
                    clip_path = clips_dir / clip.file_name
                    size_mb = clip_path.stat().st_size / 1e6 if clip_path.exists() else 0
                    status = f"{size_mb:.0f} MB" if clip_path.exists() else "MISSING"
                    print(f"  {clip.label} — {status}")
                published_videos.append(PublishedVideo(
                    file_name=video.file_name,
                    r2_url="",
                    thumbnail_url="",
                    duration=video.duration,
                    resolution=video.resolution,
                    markers=video.markers,
                    clips=[],
                    analytics=video.analytics or None,
                ))
                continue

            # Upload thumbnail
            thumb_url = ""
            if video.cover_path and Path(video.cover_path).exists():
                thumb_key = f"kickreel/sessions/{date_slug}/thumbs/{Path(video.cover_path).name}"
                thumb_url = upload_file(Path(video.cover_path), thumb_key, "image/jpeg")
                print(f"  ✓ thumbnail")

            # Upload clips
            published_clips = []
            for clip in video.clips:
                clip_path = clips_dir / clip.file_name
                if not clip_path.exists():
                    print(f"  Warning: clip not found: {clip_path}")
                    continue
                clip_key = f"kickreel/sessions/{date_slug}/clips/{clip.file_name}"
                clip_url = upload_file(clip_path, clip_key, "video/mp4")
                clip.r2_url = clip_url
                size_mb = clip_path.stat().st_size / 1e6
                print(f"  ✓ {clip.label} ({size_mb:.0f} MB)")
                clip_doc: dict = {
                    "r2Url": clip_url,
                    "startSeconds": clip.start_seconds,
                    "endSeconds": clip.end_seconds,
                    "label": clip.label,
                }
                if clip.clip_type:
                    clip_doc["clipType"] = clip.clip_type
                published_clips.append(clip_doc)

            if not published_clips:
                continue

            published_videos.append(PublishedVideo(
                file_name=video.file_name,
                r2_url="",
                thumbnail_url=thumb_url,
                duration=video.duration,
                resolution=video.resolution,
                markers=video.markers,
                clips=published_clips,
                analytics=video.analytics or None,
            ))

        if dry_run:
            print(f"\n  [dry-run] would publish {len(published_videos)} video(s) to Sanity")
            print(f"  document id would be: practiceSession-{manifest.match_id}")
            continue

        if not published_videos:
            print("  No clips to publish — skipping")
            continue

        print(f"\n  Publishing to Sanity…")
        doc_id = create_session_document(
            match_id=manifest.match_id,
            session_name=manifest.session_name,
            session_type=manifest.session_type,
            session_date=manifest.session_date,
            team_id=team_id,
            videos=published_videos,
        )
        manifest.published = True
        manifest.sanity_doc_id = doc_id
        save(session_dir, manifest)
        print(f"  ✓ {doc_id}")

    print(f"\n{'─'*50}")
    print("Publish complete.\n")
