"""Parse .track files from the Falcon XBotGo camera into analytics data."""

import re
from dataclasses import dataclass, field
from pathlib import Path


FIELD_ZONES = 10  # horizontal zones across the pitch, left goal → right goal
HALFTIME_SECONDS = 1800  # teams switch ends at 30 min


@dataclass
class ActivityBucket:
    t_start: int        # seconds from session start (relative)
    t_end: int
    detections: int     # total detect counts in window
    frames: int         # number of frames in window


@dataclass
class VideoAnalytics:
    # Full-game heatmap: zones normalized so zone 0 = left end in 1st half throughout
    field_zones: list[int] = field(default_factory=lambda: [0] * FIELD_ZONES)
    # Per-half heatmaps (same normalized orientation as field_zones)
    field_zones_first_half: list[int] = field(default_factory=lambda: [0] * FIELD_ZONES)
    field_zones_second_half: list[int] = field(default_factory=lambda: [0] * FIELD_ZONES)
    # Activity over time — 30-second buckets
    activity_buckets: list[ActivityBucket] = field(default_factory=list)
    # Near-goal pressure totals (normalized — left = left end in 1st half)
    goal_pressure_left: int = 0
    goal_pressure_right: int = 0
    # Per-half goal pressure
    goal_pressure_left_h1: int = 0
    goal_pressure_right_h1: int = 0
    goal_pressure_left_h2: int = 0
    goal_pressure_right_h2: int = 0
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

    # Frame format: UTC18:12:56.371 fps43 Frame2 Yaw-30.70 Pit0.01 Detect12
    # GoalGateLV1 format: GoalGateLV1/1Y54.5Q0.71... (Y is image coord, not field position)
    frame_pat = re.compile(
        r"UTC(\d+):(\d+):(\d+\.\d+)\s+fps\d+\s+Frame\d+\s+Yaw([-\d.]+)\s+Pit[-\d.]+\s+Detect(\d+)"
    )
    goal_pat = re.compile(r"GoalGateLV1/\d")

    lines = text.splitlines()

    # First pass: collect all frames for t0 and total count
    frame_data: list[tuple[float, float, int]] = []  # (abs_t, yaw, detect)
    for line in lines:
        fm = frame_pat.match(line)
        if fm:
            h, m, s = int(fm.group(1)), int(fm.group(2)), float(fm.group(3))
            frame_data.append((h * 3600 + m * 60 + s, float(fm.group(4)), int(fm.group(5))))

    if not frame_data:
        return None

    analytics = VideoAnalytics()
    analytics.total_frames = len(frame_data)
    t0_seconds = frame_data[0][0]

    bucket_size = 30
    buckets: dict[int, ActivityBucket] = {}

    for abs_t, yaw, detect in frame_data:
        rel_t = max(0.0, abs_t - t0_seconds)
        in_second_half = rel_t >= HALFTIME_SECONDS

        # Normalize yaw → zone 0-9; flip for 2nd half so zone 0 always = left end in 1st half
        normalized = (yaw - yaw_min) / field_range
        zone = min(FIELD_ZONES - 1, max(0, int(normalized * FIELD_ZONES)))
        if in_second_half:
            zone = (FIELD_ZONES - 1) - zone

        analytics.field_zones[zone] += 1
        if in_second_half:
            analytics.field_zones_second_half[zone] += 1
        else:
            analytics.field_zones_first_half[zone] += 1

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
    # GoalGateLV1 Y value is a vertical image coordinate (always positive) — not field side.
    # Use camera yaw at the time of the event: yaw < 0 = looking left = left goal.
    # Flip side for 2nd half to stay consistent with normalized zone orientation.
    last_abs_t: float = t0_seconds
    last_yaw: float = 0.0

    for line in lines:
        fm = frame_pat.match(line)
        if fm:
            h, m, s = int(fm.group(1)), int(fm.group(2)), float(fm.group(3))
            last_abs_t = h * 3600 + m * 60 + s
            last_yaw = float(fm.group(4))
            continue

        if goal_pat.match(line):
            rel_t = max(0.0, last_abs_t - t0_seconds)
            in_second_half = rel_t >= HALFTIME_SECONDS
            looking_left = last_yaw < 0

            # In 2nd half, teams switched: flip side so left always = 1st-half left end
            if in_second_half:
                looking_left = not looking_left

            if looking_left:
                analytics.goal_pressure_left += 1
                if in_second_half:
                    analytics.goal_pressure_left_h2 += 1
                else:
                    analytics.goal_pressure_left_h1 += 1
            else:
                analytics.goal_pressure_right += 1
                if in_second_half:
                    analytics.goal_pressure_right_h2 += 1
                else:
                    analytics.goal_pressure_right_h1 += 1

    analytics.dominant_zone = analytics.field_zones.index(max(analytics.field_zones))

    return analytics


def analytics_to_dict(a: VideoAnalytics) -> dict:
    return {
        "fieldZones": a.field_zones,
        "fieldZonesFirstHalf": a.field_zones_first_half,
        "fieldZonesSecondHalf": a.field_zones_second_half,
        "activityBuckets": [
            {"tStart": b.t_start, "tEnd": b.t_end, "detections": b.detections, "frames": b.frames}
            for b in a.activity_buckets
        ],
        "goalPressureLeft": a.goal_pressure_left,
        "goalPressureRight": a.goal_pressure_right,
        "goalPressureLeftH1": a.goal_pressure_left_h1,
        "goalPressureRightH1": a.goal_pressure_right_h1,
        "goalPressureLeftH2": a.goal_pressure_left_h2,
        "goalPressureRightH2": a.goal_pressure_right_h2,
        "totalFrames": a.total_frames,
        "dominantZone": a.dominant_zone,
    }
