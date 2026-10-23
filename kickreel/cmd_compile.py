"""Step 2b — Cut video clips for shot events detected from .track analysis."""

import subprocess
from pathlib import Path

from .clip import fmt, probe_duration
from .manifest import ClipRecord, load, save


_PAD_BEFORE = 5   # seconds before STFC rising edge
_PAD_AFTER  = 15  # seconds after — covers shot + save/goal + reset


def _cut(video_path: Path, t: float, output_path: Path, duration: float) -> bool:
    start = max(0.0, t - _PAD_BEFORE)
    end = min(duration, t + _PAD_AFTER)
    result = subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", str(start), "-to", str(end),
            "-i", str(video_path),
            "-c", "copy", "-avoid_negative_ts", "1",
            str(output_path),
        ],
        capture_output=True,
    )
    return result.returncode == 0


def run(library: Path, dry_run: bool = False, match_id: str | None = None) -> None:
    print(f"\nKickReel compile{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
    )

    if not session_dirs:
        print("No sessions found. Run 'offload', 'process', and 'analyze' first.")
        return

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        if manifest.compiled:
            print(f"[skip] {manifest.session_name} — already compiled")
            continue

        if not manifest.processed:
            print(f"[skip] {manifest.session_name} — not yet processed")
            continue

        any_shots = any(
            v.analytics.get("shotEvents") for v in manifest.videos
        )
        if not any_shots:
            print(f"[skip] {manifest.session_name} — no shot events (run 'analyze' first)")
            continue

        print(f"{'─'*50}")
        print(f"Session : {manifest.session_name}  [{manifest.session_type}]")

        clips_dir = session_dir / "clips"
        total_cut = 0

        for video in manifest.videos:
            shot_events = video.analytics.get("shotEvents", [])
            if not shot_events:
                continue

            video_path = Path(video.local_path)
            if not video_path.exists():
                print(f"  Warning: {video.file_name} not found at {video_path}")
                continue

            duration = probe_duration(video_path)

            # Goal clip marker times — use these as ground-truth goal timestamps.
            # Any shot within _SHOT_GOAL_WINDOW_S before a goal clip is skipped
            # (it already has its own goal clip).
            goal_marker_times = [
                c.marker_time for c in video.clips if c.clip_type == "goal"
            ]

            # Remove any stale shot clips from a previous compile run
            video.clips = [c for c in video.clips if c.clip_type != "shot_on_target"]

            print(f"\n  [{video.file_name}]  {len(shot_events)} shot(s)  ({len(goal_marker_times)} goal markers)")

            _SHOT_GOAL_WINDOW_S = 10  # seconds after a shot that a goal clip can confirm it

            for event in shot_events:
                t = event["timeSeconds"]
                label_time = event["label"]

                # Skip if a goal clip falls within the shot window (shot resulted in a goal)
                if any(0 <= g - t <= _SHOT_GOAL_WINDOW_S for g in goal_marker_times):
                    print(f"    [skip — goal] {label_time}")
                    continue

                label = f"Save · {label_time}"
                out = clips_dir / f"{video_path.stem}_shot_{int(t):04d}s.mp4"

                if dry_run:
                    start = max(0.0, t - _PAD_BEFORE)
                    end = min(duration, t + _PAD_AFTER)
                    print(f"    {label}  →  {fmt(start)} – {fmt(end)}")
                    continue

                if _cut(video_path, t, out, duration):
                    start = max(0.0, t - _PAD_BEFORE)
                    end = min(duration, t + _PAD_AFTER)
                    video.clips.append(ClipRecord(
                        file_name=out.name,
                        start_seconds=start,
                        end_seconds=end,
                        marker_time=t,
                        label=label,
                        clip_type="shot_on_target",
                    ))
                    total_cut += 1
                    print(f"    ✓ {label}")
                else:
                    print(f"    Warning: ffmpeg failed for shot at {fmt(t)}")

        if dry_run:
            print(f"\n  [dry-run] no files written")
        else:
            manifest.compiled = True
            save(session_dir, manifest)
            print(f"\n  ✓ {total_cut} shot clip(s) cut — manifest updated")

    print(f"\n{'─'*50}")
    print("Compile complete.\n")
