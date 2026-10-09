"""Parse .track files from the Falcon XBotGo camera into analytics data."""

import re
from dataclasses import dataclass, field, asdict
from pathlib import Path


FIELD_ZONES = 10  # horizontal zones across the pitch, left goal → right goal


@dataclass
class ActivityBucket:
    t_start: int        # seconds from session start (relative)
    t_end: int
    detections: int     # total detect counts in window
    frames: int         # number of frames in window


@dataclass
class VideoAnalytics:
    # Heatmap: how many frames the camera spent focused on each field zone (0=left, 9=right)
    field_zones: list[int] = field(default_factory=lambda: [0] * FIELD_ZONES)
    # Activity over time — 30-second buckets
    activity_buckets: list[ActivityBucket] = field(default_factory=list)
    # Near-goal pressure
    goal_pressure_left: int = 0
    goal_pressure_right: int = 0
    # Totals
    total_frames: int = 0
    dominant_zone: int = 0   # 0-9, zone with most frames


def analyze_track(track_path: Path) -> VideoAnalytics | None:
    if not track_path.exists():
        return None

    text = track_path.read_text(errors="replace")

    # --- Field calibration: extract yaw limits from SoccerFieldFilter line ---
    yaw_min, yaw_max = -62.1, 62.1  # defaults
    filter_match = re.search(r"SoccerFieldFilter yawLeftLim([-\d.]+) yawRightLim([-\d.]+)", text)
    if filter_match:
        yaw_min = -abs(float(filter_match.group(1)))
        yaw_max = abs(float(filter_match.group(2)))

    field_range = yaw_max - yaw_min

    # --- Per-frame data ---
    # Format: UTC18:12:56.371 fps43 Frame2 Yaw-30.70 Pit0.01 Detect12
    frame_pattern = re.compile(
        r"UTC(\d+):(\d+):(\d+\.\d+)\s+fps\d+\s+Frame(\d+)\s+Yaw([-\d.]+)\s+Pit[-\d.]+\s+Detect(\d+)"
    )

    frames = frame_pattern.findall(text)
    if not frames:
        return None

    analytics = VideoAnalytics()
    analytics.total_frames = len(frames)

    # Parse first frame time as session start reference
    h0, m0, s0 = int(frames[0][0]), int(frames[0][1]), float(frames[0][2])
    t0_seconds = h0 * 3600 + m0 * 60 + s0

    # Build activity buckets (30s windows)
    bucket_size = 30
    buckets: dict[int, ActivityBucket] = {}

    for h, m, s, _fnum, yaw_str, detect_str in frames:
        abs_t = int(h) * 3600 + int(m) * 60 + float(s)
        rel_t = abs_t - t0_seconds
        if rel_t < 0:
            rel_t = 0

        yaw = float(yaw_str)
        detect = int(detect_str)

        # Field zone (clamp yaw to field range, normalize to 0-9)
        normalized = (yaw - yaw_min) / field_range
        zone = min(FIELD_ZONES - 1, max(0, int(normalized * FIELD_ZONES)))
        analytics.field_zones[zone] += 1

        # Activity bucket
        bucket_idx = int(rel_t // bucket_size)
        if bucket_idx not in buckets:
            t_start = bucket_idx * bucket_size
            buckets[bucket_idx] = ActivityBucket(
                t_start=t_start,
                t_end=t_start + bucket_size,
                detections=0,
                frames=0,
            )
        buckets[bucket_idx].detections += detect
        buckets[bucket_idx].frames += 1

    analytics.activity_buckets = [buckets[k] for k in sorted(buckets)]

    # --- GoalGate pressure ---
    # Format: GoalGateLV1/1Y10.0Q...  (positive Y = right side, negative = left)
    goal_pattern = re.compile(r"GoalGateLV1/\dY([-\d.]+)")
    for m in goal_pattern.finditer(text):
        y_val = float(m.group(1))
        if y_val >= 0:
            analytics.goal_pressure_right += 1
        else:
            analytics.goal_pressure_left += 1

    analytics.dominant_zone = analytics.field_zones.index(max(analytics.field_zones))

    return analytics


def analytics_to_dict(a: VideoAnalytics) -> dict:
    return {
        "fieldZones": a.field_zones,
        "activityBuckets": [
            {"tStart": b.t_start, "tEnd": b.t_end, "detections": b.detections, "frames": b.frames}
            for b in a.activity_buckets
        ],
        "goalPressureLeft": a.goal_pressure_left,
        "goalPressureRight": a.goal_pressure_right,
        "totalFrames": a.total_frames,
        "dominantZone": a.dominant_zone,
    }
