"""Step 3 — Upload clips to R2 and publish metadata to Sanity."""

from datetime import datetime
from pathlib import Path

from . import config
from .manifest import load, save
from .publish import PublishedVideo, create_session_document, patch_compilation
from .upload import upload_clip
from .youtube import thumbnail_url as yt_thumbnail_url


def _correct_session_name(session_date: str, away_team: str) -> str:
    """Build a canonical session name from the actual recording date (not the device clock)."""
    try:
        dt = datetime.fromisoformat(session_date)
        month_day = dt.strftime("%b %-d")  # e.g. "Oct 24"
    except (ValueError, TypeError):
        month_day = "Unknown"
    return f"{month_day} - North Legends VS {away_team}"


def run(
    library: Path,
    team: str,
    dry_run: bool = False,
    match_id: str | None = None,
    compilation_path: Path | None = None,
) -> None:
    print(f"\nKickReel publish{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    team_id = config.SANITY_TEAM_U11_ID if team == "u11" else config.SANITY_TEAM_U12_ID

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
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

        # Always derive session name from actual recording date (device clock can drift)
        corrected_name = _correct_session_name(manifest.session_date, manifest.away_team)

        print(f"{'─'*50}")
        print(f"Session : {corrected_name}  [{manifest.session_type}]")

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

            # Upload clips to YouTube; derive thumbnail from first clip
            thumb_url = ""
            published_clips = []
            for clip in video.clips:
                clip_path = clips_dir / clip.file_name
                if not clip_path.exists():
                    print(f"  Warning: clip not found: {clip_path}")
                    continue
                size_mb = clip_path.stat().st_size / 1e6
                video_id, clip_url = upload_clip(clip_path, title=clip.label or clip.file_name)
                clip.r2_url = clip_url  # field stores YouTube URL going forward
                if not thumb_url:
                    thumb_url = yt_thumbnail_url(video_id)
                print(f"  ✓ {clip.label} ({size_mb:.0f} MB) → {clip_url}")
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

        doc_type = "gameSession" if manifest.session_type == "GAME" else "practiceSession"
        existing_id = manifest.sanity_doc_id or ""
        expected_id = existing_id or f"{doc_type}-{manifest.match_id}"

        if dry_run:
            print(f"\n  [dry-run] would publish {len(published_videos)} video(s) to Sanity")
            print(f"  document id would be: {expected_id}")
            continue

        if not published_videos:
            print("  No clips to publish — skipping")
            continue

        print(f"\n  Publishing to Sanity…")
        doc_id = create_session_document(
            match_id=manifest.match_id,
            session_name=corrected_name,
            session_type=manifest.session_type,
            session_date=manifest.session_date,
            team_id=team_id,
            videos=published_videos,
            home_team="",   # already baked into corrected_name
            away_team="",
            existing_doc_id=existing_id,
        )
        manifest.published = True
        manifest.sanity_doc_id = doc_id

        # ── Compilation upload ────────────────────────────────────────────────
        # Prefer explicit --compilation arg; fall back to manifest.compilation_path
        comp_path = compilation_path or (
            Path(manifest.compilation_path) if manifest.compilation_path else None
        )
        if comp_path and comp_path.exists():
            print(f"  Uploading compilation…")
            try:
                dt = datetime.fromisoformat(manifest.session_date)
                label_date = dt.strftime("%b %-d")
            except (ValueError, TypeError):
                label_date = date_slug
            comp_title = f"Goals Compilation · {label_date} vs {manifest.away_team}"
            _, comp_url = upload_clip(comp_path, title=comp_title)
            try:
                dt = datetime.fromisoformat(manifest.session_date)
                label_date = dt.strftime("%b %-d")
            except (ValueError, TypeError):
                label_date = date_slug
            patch_compilation(doc_id, comp_url, comp_title)
            manifest.compilation_path = str(comp_path)
            print(f"  ✓ compilation → {comp_url}")
            print(f"  ✓ compilationTitle: {comp_title}")
        elif comp_path:
            print(f"  Warning: compilation file not found: {comp_path}")

        save(session_dir, manifest)
        print(f"  ✓ {doc_id}")

    print(f"\n{'─'*50}")
    print("Publish complete.\n")
