"""Step 2 — Extract highlight clips from offloaded footage."""

from pathlib import Path

from .clip import extract_clips, fmt
from .manifest import ClipRecord, load, save


def run(library: Path, pad: int = 15) -> None:
    print(f"\nKickReel process: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
    )

    if not session_dirs:
        print("No sessions found. Run 'offload' first.")
        return

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        if manifest.processed:
            print(f"[skip] {manifest.session_name} — already processed")
            continue

        if not manifest.offloaded:
            print(f"[skip] {manifest.session_name} — not yet offloaded")
            continue

        print(f"{'─'*50}")
        print(f"Session : {manifest.session_name}  [{manifest.session_type}]")

        clips_dir = session_dir / "clips"
        any_clips = False

        for video in manifest.videos:
            video_path = Path(video.local_path)
            if not video_path.exists():
                print(f"  Warning: {video.file_name} not found at {video_path}")
                continue

            if not video.markers:
                print(f"  [{video.file_name}] no markers — skipping")
                continue

            print(f"\n  [{video.file_name}]  {video.resolution}  {video.duration:.0f}s")
            print(f"  Markers: {', '.join(str(m['label']) for m in video.markers)}")

            # Build MarkerEvent-like objects from the manifest records
            class _M:
                def __init__(self, t: float):
                    self.type = "click"
                    self.time_seconds = t

            marker_objs = [_M(m["timeSeconds"]) for m in video.markers]
            clips = extract_clips(video_path, marker_objs, clips_dir, pad)  # type: ignore[arg-type]
            print(f"  → {len(clips)} clip(s) extracted")

            video.clips = [
                ClipRecord(
                    file_name=clip.output_path.name,
                    start_seconds=clip.start_seconds,
                    end_seconds=clip.end_seconds,
                    marker_time=clip.marker_time,
                    label=clip.label,
                )
                for clip in clips
            ]
            if clips:
                any_clips = True

        if any_clips:
            manifest.processed = True
            save(session_dir, manifest)
            print(f"\n  ✓ Manifest updated")
        else:
            print(f"\n  No clips produced — manifest unchanged")

    print(f"\n{'─'*50}")
    print("Processing complete.\n")
