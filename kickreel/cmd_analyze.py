"""Step 2b — Analyze .track files and store analytics in the session manifest."""

from pathlib import Path

from .analyze import analyze_track, analytics_to_dict
from .manifest import load, save


def run(library: Path, dry_run: bool = False) -> None:
    print(f"\nKickReel analyze{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
    )

    if not session_dirs:
        print("No sessions found. Run 'offload' first.")
        return

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest or not manifest.offloaded:
            continue

        print(f"{'─'*50}")
        print(f"Session : {manifest.session_name}  [{manifest.session_type}]")

        any_analytics = False
        for video in manifest.videos:
            if not video.track_path:
                print(f"  [{video.file_name}] no track file — skipping")
                continue

            track_path = Path(video.track_path)
            analytics = analyze_track(track_path)
            if not analytics:
                print(f"  [{video.file_name}] could not parse track file")
                continue

            print(f"\n  [{video.file_name}]")
            print(f"  Frames      : {analytics.total_frames}")
            print(f"  Field zones : {analytics.field_zones}  (dominant: {analytics.dominant_zone})")
            print(f"  Goal press  : L={analytics.goal_pressure_left}  R={analytics.goal_pressure_right}")
            print(f"  Buckets     : {len(analytics.activity_buckets)} × 30s")

            if not dry_run:
                video.analytics = analytics_to_dict(analytics)
                any_analytics = True

        if dry_run:
            print(f"\n  [dry-run] no files written")
        elif any_analytics:
            save(session_dir, manifest)
            print(f"\n  ✓ Manifest updated with analytics")
        else:
            print(f"\n  No analytics produced")

    print(f"\n{'─'*50}")
    print("Analysis complete.\n")
