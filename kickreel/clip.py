import subprocess
from dataclasses import dataclass
from pathlib import Path

from .ingest import MarkerEvent


@dataclass
class Clip:
    video_file_name: str
    output_path: Path
    start_seconds: float
    end_seconds: float
    marker_time: float
    label: str


def fmt(seconds: float) -> str:
    m = int(seconds) // 60
    s = int(seconds) % 60
    return f"{m}:{s:02d}"


def extract_clips(
    video_path: Path,
    markers: list[MarkerEvent],
    output_dir: Path,
    pad: int = 15,
) -> list[Clip]:
    output_dir.mkdir(parents=True, exist_ok=True)
    duration = _probe_duration(video_path)
    clips: list[Clip] = []
    seen: list[float] = []
    clip_num = 0

    for marker in markers:
        if marker.type != "click":
            continue
        t = marker.time_seconds
        if any(abs(t - s) < 1.0 for s in seen):
            continue
        seen.append(t)
        clip_num += 1

        start = max(0.0, t - pad)
        end = min(duration, t + pad)
        out = output_dir / f"{video_path.stem}_clip_{int(t):04d}s.mp4"

        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start),
                "-to", str(end),
                "-i", str(video_path),
                "-c", "copy",
                "-avoid_negative_ts", "1",
                str(out),
            ],
            capture_output=True,
        )
        if result.returncode != 0:
            print(f"    Warning: ffmpeg failed for clip at {fmt(t)}")
            continue

        clips.append(Clip(
            video_file_name=video_path.name,
            output_path=out,
            start_seconds=start,
            end_seconds=end,
            marker_time=t,
            label=f"Clip {clip_num} · {fmt(t)}",
        ))

    return clips


def _probe_duration(video_path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    try:
        return float(result.stdout.strip())
    except ValueError:
        return 0.0
