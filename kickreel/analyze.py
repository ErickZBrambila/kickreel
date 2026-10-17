"""Parse .track files from the Falcon XBotGo camera into analytics data."""

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from scipy.signal import find_peaks


FIELD_ZONES = 10        # horizontal zones across the pitch, left goal → right goal
HALFTIME_SECONDS = 1800 # teams switch ends at 30 min

# Goal detection tuning
_BALL_GATE_D_THRESHOLD = 2.0  # d ≤ this → ball at/inside goal mouth
_MIN_RUN_FRAMES = 4           # consecutive near-goal frames to form a candidate
_MERGE_GAP_S = 30             # merge two candidates closer than this
_STARTUP_GRACE_S = 60         # ignore candidates in first N seconds (camera calibrating)
_CROWD_BASELINE_S = 15        # look back this far for pre-event crowd baseline
_CROWD_WINDOW_S = 12          # look forward this far for post-event celebration


@dataclass
class ActivityBucket:
    t_start: int        # seconds from session start (relative)
    t_end: int
    detections: int     # total detect counts in window
    frames: int         # number of frames in window


@dataclass
class GoalEvent:
    time_seconds: float       # video-relative seconds
    label: str                # "MM:SS" formatted
    side: str                 # "left" | "right" (normalized: left = 1st-half left end)
    confidence: float         # 0.0–1.0
    ball_gate_min_d: float    # minimum BallGate distance during the run
    gate_visible: bool        # GoalGate detected during the run
    crowd_spike_pct: float    # % increase in detections after event vs before
    ball_lost_after: bool     # ball tracking dropped after the near-gate run


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
    # Detected goal events
    goal_events: list[GoalEvent] = field(default_factory=list)
    # Totals
    total_frames: int = 0
    dominant_zone: int = 0   # 0-9, zone with most frames


# --- Compiled patterns (module-level for performance) ---

_frame_pat = re.compile(
    r"UTC(\d+):(\d+):(\d+\.\d+)\s+fps\d+\s+Frame\d+\s+Yaw([-\d.]+)\s+Pit[-\d.]+\s+Detect(\d+)(.*)"
)
_ballgate_inline = re.compile(r"BallGate d([\d.]+)/[\d.]+W near(\d+)")
_goalgate_any = re.compile(r"GoalGate[LR]V[01]/\d")   # LV0, LV1, RV0, RV1
_ball_tracked_pat = re.compile(r"^Ball[12] (?!Fail)")


@dataclass
class _FrameCtx:
    """Parsed per-frame data used by the goal detector."""
    rel_t: float
    yaw: float
    detect: int
    ball_gate_d: float | None = None
    ball_gate_near: bool = False
    goal_gate_visible: bool = False
    ball_tracked: bool = False


def _fmt(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def _score_run(
    run: list[_FrameCtx],
    all_frames: list[_FrameCtx],
    run_end_idx: int,
    in_second_half: bool,
) -> GoalEvent:
    """Score a ball-near-gate run and return a GoalEvent."""
    t = run[0].rel_t
    yaw = sum(f.yaw for f in run) / len(run)

    # Side: looking_left means left goal (in 1st-half orientation)
    looking_left = yaw < 0
    if in_second_half:
        looking_left = not looking_left
    side = "left" if looking_left else "right"

    min_d = min((f.ball_gate_d for f in run if f.ball_gate_d is not None), default=99.0)
    gate_visible = any(f.goal_gate_visible for f in run)
    n_frames = len(run)

    # Ball lost after: check next 20 frames for Ball0 Fail predominance
    post_frames = all_frames[run_end_idx: run_end_idx + 20]
    ball_lost_after = (
        len(post_frames) >= 5
        and sum(1 for f in post_frames if not f.ball_tracked) / len(post_frames) >= 0.7
    )

    # Crowd spike: use find_peaks on the post-event Detect series vs pre-event baseline.
    # A real celebration produces a sharp, prominent peak in the detection count.
    pre = [f.detect for f in all_frames if t - _CROWD_BASELINE_S <= f.rel_t < t]
    post = [f.detect for f in all_frames if t < f.rel_t <= t + _CROWD_WINDOW_S]
    baseline = float(np.mean(pre)) if pre else 0.0
    crowd_spike_pct = 0.0
    if post and len(post) >= 3:
        post_arr = np.array(post, dtype=float)
        peaks, props = find_peaks(
            post_arr,
            prominence=max(2.0, baseline * 0.10),  # must rise >10% above baseline
            width=1,
        )
        if len(peaks):
            best_peak = float(post_arr[peaks].max())
            crowd_spike_pct = ((best_peak - baseline) / max(baseline, 1)) * 100

    # Confidence scoring
    conf = 0.0
    if min_d <= 1.0:
        conf += 0.35
    elif min_d <= 1.5:
        conf += 0.25
    else:
        conf += 0.10

    if gate_visible:
        conf += 0.20
    if ball_lost_after:
        conf += 0.15
    if crowd_spike_pct >= 15:
        conf += min(0.15, crowd_spike_pct / 100)
    if n_frames >= 10:
        conf += 0.15
    elif n_frames >= 6:
        conf += 0.08

    return GoalEvent(
        time_seconds=t,
        label=_fmt(t),
        side=side,
        confidence=min(1.0, conf),
        ball_gate_min_d=min_d,
        gate_visible=gate_visible,
        crowd_spike_pct=round(crowd_spike_pct, 1),
        ball_lost_after=ball_lost_after,
    )


def _detect_goal_events(frames: list[_FrameCtx], half_offset: float = 0.0) -> list[GoalEvent]:
    """Find goal candidates from per-frame data and return merged, sorted events."""
    candidates: list[GoalEvent] = []
    run: list[_FrameCtx] = []
    run_start_idx = 0

    for i, f in enumerate(frames):
        in_run = (
            f.ball_gate_near
            and f.ball_gate_d is not None
            and f.ball_gate_d <= _BALL_GATE_D_THRESHOLD
        )
        if in_run:
            if not run:
                run_start_idx = i
            run.append(f)
        else:
            if len(run) >= _MIN_RUN_FRAMES:
                in_h2 = (run[0].rel_t + half_offset) >= HALFTIME_SECONDS
                candidates.append(_score_run(run, frames, i, in_h2))
            run = []

    if len(run) >= _MIN_RUN_FRAMES:
        in_h2 = (run[0].rel_t + half_offset) >= HALFTIME_SECONDS
        candidates.append(_score_run(run, frames, len(frames), in_h2))

    # Merge candidates within _MERGE_GAP_S of each other (keep highest confidence)
    candidates.sort(key=lambda e: e.time_seconds)
    # Drop anything in the startup window — camera is still calibrating field filter
    candidates = [c for c in candidates if c.time_seconds >= _STARTUP_GRACE_S]
    merged: list[GoalEvent] = []
    for cand in candidates:
        if merged and cand.time_seconds - merged[-1].time_seconds < _MERGE_GAP_S:
            if cand.confidence > merged[-1].confidence:
                merged[-1] = cand
        else:
            merged.append(cand)

    return merged


def analyze_track(track_path: Path, video_duration: float | None = None, half_offset: float = 0.0) -> VideoAnalytics | None:
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
    lines = text.splitlines()

    analytics = VideoAnalytics()
    bucket_size = 30
    buckets: dict[int, ActivityBucket] = {}

    # Single pass: build per-frame contexts and accumulate all analytics simultaneously
    frames: list[_FrameCtx] = []
    current: _FrameCtx | None = None
    t0_seconds: float | None = None

    for line in lines:
        fm = _frame_pat.match(line)
        if fm:
            h, m, s = int(fm.group(1)), int(fm.group(2)), float(fm.group(3))
            abs_t = h * 3600 + m * 60 + s
            if t0_seconds is None:
                t0_seconds = abs_t

            rel_t = max(0.0, abs_t - t0_seconds)
            yaw = float(fm.group(4))
            detect = int(fm.group(5))
            rest = fm.group(6)

            current = _FrameCtx(rel_t=rel_t, yaw=yaw, detect=detect)

            # BallGate is inline on the frame line
            bg = _ballgate_inline.search(rest)
            if bg:
                current.ball_gate_d = float(bg.group(1))
                current.ball_gate_near = int(bg.group(2)) >= 1

            frames.append(current)
            analytics.total_frames += 1

            # Field zone accumulation — use game_t (= rel_t + offset) for half assignment
            game_t = rel_t + half_offset
            in_second_half = game_t >= HALFTIME_SECONDS
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

            continue

        if current is None:
            continue

        # GoalGate: LV0, LV1, RV0, RV1 — fix: track both sides
        if _goalgate_any.match(line):
            current.goal_gate_visible = True

            rel_t = current.rel_t
            in_second_half = (rel_t + half_offset) >= HALFTIME_SECONDS
            looking_left = current.yaw < 0
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
            continue

        # Ball tracking status
        if line.startswith("Ball"):
            if _ball_tracked_pat.match(line):
                current.ball_tracked = True

    if not frames:
        return None

    analytics.activity_buckets = [buckets[k] for k in sorted(buckets)]
    analytics.dominant_zone = analytics.field_zones.index(max(analytics.field_zones))

    goal_frames = frames if video_duration is None else [f for f in frames if f.rel_t <= video_duration]
    analytics.goal_events = _detect_goal_events(goal_frames, half_offset=half_offset)

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
        "goalEvents": [
            {
                "timeSeconds": e.time_seconds,
                "label": e.label,
                "side": e.side,
                "confidence": round(e.confidence, 3),
                "ballGateMinD": e.ball_gate_min_d,
                "gateVisible": e.gate_visible,
                "crowdSpikePct": e.crowd_spike_pct,
                "ballLostAfter": e.ball_lost_after,
            }
            for e in a.goal_events
        ],
        "totalFrames": a.total_frames,
        "dominantZone": a.dominant_zone,
    }
