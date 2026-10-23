"""Step 2b — Analyze .track files and store analytics in the session manifest."""

from pathlib import Path

from .analyze import analyze_track, analytics_to_dict
from .manifest import load, save


def run(library: Path, dry_run: bool = False, match_id: str | None = None, attack_direction: str | None = None) -> None:
    print(f"\nKickReel analyze{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
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

        # Store attack direction in manifest if provided via CLI
        if attack_direction is not None and manifest.h1_attack_side != attack_direction:
            manifest.h1_attack_side = attack_direction
            print(f"  h1_attack_side set to '{attack_direction}'")

        effective_attack_side = manifest.h1_attack_side  # use manifest value (may have been set previously)
        if effective_attack_side:
            print(f"  Attack filter: H1={effective_attack_side}  H2={'left' if effective_attack_side == 'right' else 'right'}")

        any_analytics = False
        for video in manifest.videos:
            if not video.track_path:
                print(f"  [{video.file_name}] no track file — skipping")
                continue

            track_path = Path(video.track_path)
            analytics = analyze_track(
                track_path,
                video_duration=video.duration or None,
                half_offset=video.half_offset_seconds,
                h1_attack_side=effective_attack_side,
            )
            if not analytics:
                print(f"  [{video.file_name}] could not parse track file")
                continue

            print(f"\n  [{video.file_name}]")
            print(f"  Frames      : {analytics.total_frames}")
            print(f"  Field zones : {analytics.field_zones}  (dominant: {analytics.dominant_zone})")
            print(f"  Goal press  : L={analytics.goal_pressure_left}  R={analytics.goal_pressure_right}")
            print(f"  Buckets     : {len(analytics.activity_buckets)} × 30s")

            # Goal events — filter to video duration, sorted by confidence
            dur = video.duration
            in_video = [e for e in analytics.goal_events if e.time_seconds <= dur]
            if in_video:
                print(f"  Goal events : {len(in_video)} candidate(s)")
                for e in sorted(in_video, key=lambda x: -x.confidence):
                    markers = [m["timeSeconds"] for m in video.markers]
                    near = min((abs(e.time_seconds - ms) for ms in markers), default=999)
                    near_flag = f"  ← marker {near:.0f}s away" if near <= 60 else ""
                    print(
                        f"    {e.label:>6}  conf={e.confidence:.2f}  {e.side:5}  "
                        f"minD={e.ball_gate_min_d:.1f}  crowd={e.crowd_spike_pct:+.0f}%  "
                        f"ballLost={e.ball_lost_after}{near_flag}"
                    )
            else:
                print(f"  Goal events : none detected")

            # Shot events
            shot_in_video = [e for e in analytics.shot_events if e.time_seconds <= (video.duration or float("inf"))]
            goals_from_shots = sum(1 for e in shot_in_video if e.resulted_in_goal)
            saves = analytics.saves
            print(f"  Shots       : {analytics.shots_on_target} on target  ({saves} saves, {goals_from_shots} goals)")
            if shot_in_video:
                first_half = [e for e in shot_in_video if e.time_seconds < 2400]
                second_half = [e for e in shot_in_video if e.time_seconds >= 2400]
                print(f"    H1: {len(first_half)} shots  H2: {len(second_half)} shots")

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
