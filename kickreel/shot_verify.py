"""Shot-on-target verification using track-file trajectory features + optional HSV jersey calibration.

Algorithm
---------
For each Ball TSz candidate shot time:

  Pass 1 — track-file geometry (always runs):
    1. Launch angle   — from xv/yv velocity components; > 50° = clearance/lob
    2. Ball height    — ball_y pixel; goal mouth is ~400–1050 px in 1080p
    3. Track length   — RB consecutive frames; < 7 = unreliable blip

  Pass 2 — HSV jersey check (runs when video_path + goal_times are provided):
    Auto-calibrates jersey HSV from frames at known goal times (our team is
    definitely near the ball there).  At each candidate, checks whether the
    calibrated jersey color is present within ~150 px of ball(x,y).
    Rejects if jersey is absent AND the candidate already looks marginal.

Usage (standalone)
------------------
    python -m kickreel.shot_verify \
        --track path/to/video.track \
        --manifest path/to/kickreel.json \
        [--video path/to/video.mp4]

Integration
-----------
    from kickreel.shot_verify import verify_shots
    confirmed = [r for r in verify_shots(
        times, track_path,
        video_path=video_path,
        goal_times=goal_marker_times,
    ) if r.verdict == "verified"]
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    _CV2_AVAILABLE = False

HALFTIME_SECONDS = 2200  # kept in sync with analyze.py

# ── Thresholds ──────────────────────────────────────────────────────────────────
_MAX_LAUNCH_ANGLE = 50.0   # °  above horizontal; > 50° = overhead clearance / lob
_MIN_RB           = 7      # min consecutive tracking frames
_MAX_BALL_Y       = 1050   # px  goal mouth upper bound (1080p)
_MIN_BALL_Y       = 350    # px  goal mouth lower bound

# HSV jersey calibration
_GRASS_H_LO  = 35    # hue range to mask out (yellow-green to green = grass)
_GRASS_H_HI  = 85
_GRASS_S_MIN = 40
_GRASS_V_MIN = 50
_JERSEY_RADIUS  = 150   # px radius around ball to search for jersey pixels
_JERSEY_SAT_MIN = 80    # minimum saturation to count as "jersey" pixel
_JERSEY_HUE_SLACK = 20  # ± hue degrees around dominant calibrated hue
_JERSEY_MIN_PIXELS = 12  # must find at least N jersey pixels to call it a hit (ball-radius check)
_JERSEY_MIN_PIXELS_HALF = 3000  # threshold for half-frame scan (mark-only candidates)

# Jersey color hint → (OpenCV hue lo, OpenCV hue hi) search window(s)
# Excludes grass (35-85) and sky/blue (88-130) confounds automatically
_JERSEY_COLOR_RANGES: dict[str, list[tuple[int, int]]] = {
    "red":    [(0, 20), (160, 179)],   # red wraps around 0; outdoor shifts to orange
    "orange": [(5, 30)],
    "yellow": [(20, 40)],
    "blue":   [(90, 130)],
    "green":  [(40, 80)],              # distinct from grass by saturation/value
    "purple": [(130, 160)],
    "white":  [],                       # low-saturation; not calibratable by hue
    "black":  [],
}


@dataclass
class JerseyCalibration:
    hsv_lower: "np.ndarray"
    hsv_upper: "np.ndarray"
    dominant_hue: int
    sample_count: int
    # Red wraps in HSV — store optional second range for hue near 0/179
    hsv_lower2: "np.ndarray | None" = None
    hsv_upper2: "np.ndarray | None" = None


@dataclass
class ShotVerifyResult:
    time_seconds: float
    angle_deg: float
    ball_y: int
    rb: int
    verdict: str   # "verified" | "reject_angle" | "reject_rb" | "reject_y" | "reject_jersey"
    note: str = ""
    jersey_pixels: int = 0


# ── Track-file parser ────────────────────────────────────────────────────────────

def _parse_track_frames(
    track_path: Path,
) -> list[tuple[float, int, int, int, int, int, int, int]]:
    """Return (rel_t, speed, video_x, video_y, rb, outx, xv, yv) per Ball TSz frame.

    ball(x,y) in the track file is in panoramic sensor coordinates.  The
    ``Crowd`` line's ``realx`` value is the camera's current pan position
    (= center of the video frame in panoramic space).  We convert:

        video_x = panoramic_bx - realx + 960

    where 960 is half the 1920px frame width.  video_y is used as-is because
    the camera pans horizontally only.
    """
    frame_pat = re.compile(
        r"UTC(\d+):(\d+):(\d+\.\d+)\s+fps\d+\s+Frame\d+\s+Yaw([-\d.]+)"
    )
    crowd_pat = re.compile(r"Crowd\d+.*realx(\d+)")
    tsz_pat = re.compile(
        r"Ball\d TSz\d+ \(T\d+ RB(\d+) xv(-?\d+) yv(-?\d+) m(\d+)\)"
        r".*ball\((-?\d+),(-?\d+)\) outx(-?\d+)"
    )

    t0: float | None = None
    current_rel_t: float = 0.0
    current_realx: int = 1280   # default: camera centered on panoramic midpoint
    frames: list[tuple[float, int, int, int, int, int, int, int]] = []

    for line in track_path.read_text(errors="replace").splitlines():
        fm = frame_pat.match(line)
        if fm:
            h, m, s = int(fm.group(1)), int(fm.group(2)), float(fm.group(3))
            abs_t = h * 3600 + m * 60 + s
            if t0 is None:
                t0 = abs_t
            current_rel_t = abs_t - t0
            continue
        cm = crowd_pat.match(line)
        if cm:
            current_realx = int(cm.group(1))
            continue
        bm = tsz_pat.search(line)
        if bm:
            rb, xv, yv, speed, pan_bx, by, outx = (int(bm.group(i)) for i in range(1, 8))
            video_x = pan_bx - current_realx + 960
            frames.append((current_rel_t, speed, video_x, by, rb, outx, xv, yv))

    return frames


def _cluster_features(
    frames: list[tuple[float, int, int, int, int, int, int, int]],
    shot_time: float,
    window: float = 2.0,
) -> tuple[float, int, int, int, int, float]:
    """Return (angle_deg, ball_y, rb, ball_x, ball_y_raw, peak_frame_time).

    The last element is the timestamp of the peak-speed frame — use it to extract
    the video frame so the ball position and frame are in sync.
    """
    nearby = [f for f in frames if abs(f[0] - shot_time) <= window]
    if not nearby:
        return 0.0, 700, 10, 960, 700, shot_time

    peak = max(nearby, key=lambda f: f[1])
    peak_t, speed, bx, by, rb, outx, xv, yv = peak
    angle = math.degrees(math.atan2(abs(yv), abs(xv))) if abs(xv) > 0 else 90.0
    return angle, by, rb, bx, by, peak_t


# ── HSV jersey calibration ───────────────────────────────────────────────────────

def _extract_frame(video_path: Path, time_seconds: float) -> "np.ndarray | None":
    if not _CV2_AVAILABLE:
        return None
    cap = cv2.VideoCapture(str(video_path))
    cap.set(cv2.CAP_PROP_POS_MSEC, time_seconds * 1000)
    ok, frame = cap.read()
    cap.release()
    return frame if ok else None


def _sample_pixels_near_ball(
    frame: "np.ndarray",
    bx: int,
    by: int,
    radius: int,
) -> "np.ndarray":
    """Return non-grass HSV pixels within radius of ball position."""
    h, w = frame.shape[:2]
    bx = max(radius, min(w - radius, bx))
    by = max(radius, min(h - radius, by))

    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (bx, by), radius, 255, -1)

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    # Mask out grass (green/yellow-green)
    grass_mask = cv2.inRange(
        hsv,
        np.array([_GRASS_H_LO, _GRASS_S_MIN, _GRASS_V_MIN]),
        np.array([_GRASS_H_HI, 255, 255]),
    )
    combined = cv2.bitwise_and(mask, cv2.bitwise_not(grass_mask))
    return hsv[combined > 0]


def calibrate_jersey_hsv(
    video_path: Path,
    goal_times: list[float],
    ball_positions: dict[float, tuple[int, int]],
    radius: int = _JERSEY_RADIUS,
    verbose: bool = False,
) -> JerseyCalibration | None:
    """Auto-calibrate jersey HSV from frames at known goal times.

    Our team is guaranteed to be near the ball at goal moments, so sampling
    those frames captures the actual jersey color under game lighting conditions.
    """
    if not _CV2_AVAILABLE:
        print("  [jersey-cal] cv2 not installed — skipping HSV calibration")
        return None
    if not goal_times:
        print("  [jersey-cal] no goal times provided — skipping")
        return None

    all_pixels: list["np.ndarray"] = []

    for t in goal_times:
        # Find nearest tracked ball position
        if not ball_positions:
            continue
        nearest_t = min(ball_positions, key=lambda x: abs(x - t))
        if abs(nearest_t - t) > 8.0:
            continue
        bx, by = ball_positions[nearest_t]

        # Try frame at the goal moment and 1s before (player still visible)
        for offset in (0.0, -1.0, 1.0):
            frame = _extract_frame(video_path, t + offset)
            if frame is None:
                continue
            pixels = _sample_pixels_near_ball(frame, bx, by, radius)
            if len(pixels) >= 30:
                sat_mask = pixels[:, 1] >= _JERSEY_SAT_MIN
                saturated = pixels[sat_mask]
                if len(saturated) >= 20:
                    all_pixels.append(saturated)
                    break

    if not all_pixels:
        if verbose:
            print("  [jersey-cal] could not sample enough pixels — calibration failed")
        return None

    combined = np.vstack(all_pixels)
    hues = combined[:, 0].astype(np.float32)

    # Find dominant hue bin (OpenCV HSV: hue 0–179)
    hist, edges = np.histogram(hues, bins=36, range=(0, 180))
    dominant_bin = int(np.argmax(hist))
    dominant_hue = int((edges[dominant_bin] + edges[dominant_bin + 1]) / 2)

    lo = np.array([max(0, dominant_hue - _JERSEY_HUE_SLACK), _JERSEY_SAT_MIN, 50])
    hi = np.array([min(179, dominant_hue + _JERSEY_HUE_SLACK), 255, 255])

    # Handle red hue wrap-around (hue near 0 or 179)
    lo2 = hi2 = None
    if dominant_hue - _JERSEY_HUE_SLACK < 0:
        lo2 = np.array([180 + (dominant_hue - _JERSEY_HUE_SLACK), _JERSEY_SAT_MIN, 50])
        hi2 = np.array([179, 255, 255])
    elif dominant_hue + _JERSEY_HUE_SLACK > 179:
        lo2 = np.array([0, _JERSEY_SAT_MIN, 50])
        hi2 = np.array([(dominant_hue + _JERSEY_HUE_SLACK) - 180, 255, 255])

    cal = JerseyCalibration(
        hsv_lower=lo,
        hsv_upper=hi,
        dominant_hue=dominant_hue,
        sample_count=len(combined),
        hsv_lower2=lo2,
        hsv_upper2=hi2,
    )

    if verbose:
        deg = dominant_hue * 2  # OpenCV hue is 0-179, multiply by 2 for 0-360°
        print(f"  [jersey-cal] dominant hue={dominant_hue} ({deg}°)  "
              f"range=[{lo[0]}–{hi[0]}]  pixels={len(combined)}")
    return cal


def calibrate_from_xbot_clips(
    clip_paths: list[Path],
    jersey_color: str = "red",
    sample_offsets: tuple[float, ...] = (0.3, 0.8, 1.3),
    verbose: bool = False,
) -> JerseyCalibration | None:
    """Calibrate jersey HSV from XBotGo confirmed-shot clips.

    Each clip is a close-up of a confirmed shot by our team — the shooter
    (our jersey color) is the dominant non-grass subject in the first second.
    We sample early frames before the outcome, mask grass, and find the
    dominant saturated color cluster.
    """
    if not _CV2_AVAILABLE:
        print("  [jersey-cal] cv2 not installed — skipping")
        return None

    all_pixels: list["np.ndarray"] = []

    for clip_path in clip_paths:
        for offset in sample_offsets:
            frame = _extract_frame(clip_path, offset)
            if frame is None:
                continue

            h, w = frame.shape[:2]
            # Sample center strip: full width, middle 60% height (players, not sky)
            roi = frame[int(h * 0.2):int(h * 0.8), :]
            hsv_roi = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)

            # Mask grass
            grass_mask = cv2.inRange(
                hsv_roi,
                np.array([_GRASS_H_LO, _GRASS_S_MIN, _GRASS_V_MIN]),
                np.array([_GRASS_H_HI, 255, 255]),
            )
            non_grass = hsv_roi[grass_mask == 0]

            # Keep only saturated pixels (jersey colors, not white/grey kit or dirt)
            if len(non_grass) > 0:
                sat_mask = non_grass[:, 1] >= _JERSEY_SAT_MIN
                saturated = non_grass[sat_mask]
                if len(saturated) >= 20:
                    all_pixels.append(saturated)
            break  # one good frame per clip is enough

    if not all_pixels:
        if verbose:
            print("  [jersey-cal] no usable frames from XBotGo clips")
        return None

    combined = np.vstack(all_pixels)
    hues = combined[:, 0].astype(np.float32)

    hist, edges = np.histogram(hues, bins=36, range=(0, 180))

    # Restrict search to jersey color family if a hint is provided
    color_ranges = _JERSEY_COLOR_RANGES.get(jersey_color.lower(), [])
    if color_ranges:
        restricted = hist.copy()
        for i, (lo_e, hi_e) in enumerate(zip(edges[:-1], edges[1:])):
            in_range = any(rlo <= lo_e and hi_e <= rhi for rlo, rhi in color_ranges)
            if not in_range:
                restricted[i] = 0
        if restricted.max() > 0:
            hist = restricted
        elif verbose:
            print(f"  [jersey-cal/xbot] no '{jersey_color}' pixels found — using dominant hue")

    dominant_bin = int(np.argmax(hist))
    dominant_hue = int((edges[dominant_bin] + edges[dominant_bin + 1]) / 2)

    lo = np.array([max(0, dominant_hue - _JERSEY_HUE_SLACK), _JERSEY_SAT_MIN, 50])
    hi = np.array([min(179, dominant_hue + _JERSEY_HUE_SLACK), 255, 255])

    lo2 = hi2 = None
    if dominant_hue - _JERSEY_HUE_SLACK < 0:
        lo2 = np.array([180 + (dominant_hue - _JERSEY_HUE_SLACK), _JERSEY_SAT_MIN, 50])
        hi2 = np.array([179, 255, 255])
    elif dominant_hue + _JERSEY_HUE_SLACK > 179:
        lo2 = np.array([0, _JERSEY_SAT_MIN, 50])
        hi2 = np.array([(dominant_hue + _JERSEY_HUE_SLACK) - 180, 255, 255])

    cal = JerseyCalibration(
        hsv_lower=lo,
        hsv_upper=hi,
        dominant_hue=dominant_hue,
        sample_count=len(combined),
        hsv_lower2=lo2,
        hsv_upper2=hi2,
    )

    if verbose:
        deg = dominant_hue * 2
        print(f"  [jersey-cal/xbot] dominant hue={dominant_hue} ({deg}°)  "
              f"range=[{lo[0]}–{hi[0]}]  pixels={len(combined)}  clips={len(clip_paths)}")
    return cal


def _count_jersey_pixels(
    frame: "np.ndarray",
    bx: int,
    by: int,
    cal: JerseyCalibration,
    radius: int = _JERSEY_RADIUS,
) -> int:
    """Count pixels matching calibrated jersey color within radius of ball.

    ``bx`` and ``by`` are in 1920×1080 coordinate space (track file native).
    They are scaled to the actual frame dimensions before searching — handles
    4K (3840×2160) video transparently.
    """
    h, w = frame.shape[:2]
    scale_x = w / 1920
    scale_y = h / 1080
    bx_s = int(bx * scale_x)
    by_s = int(by * scale_y)
    r_s = int(radius * max(scale_x, scale_y))
    bx_c = max(r_s, min(w - r_s, bx_s))
    by_c = max(r_s, min(h - r_s, by_s))
    mask = np.zeros((h, w), dtype=np.uint8)
    cv2.circle(mask, (bx_c, by_c), r_s, 255, -1)

    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    jersey = cv2.inRange(hsv, cal.hsv_lower, cal.hsv_upper)
    if cal.hsv_lower2 is not None and cal.hsv_upper2 is not None:
        jersey2 = cv2.inRange(hsv, cal.hsv_lower2, cal.hsv_upper2)
        jersey = cv2.bitwise_or(jersey, jersey2)

    return int(cv2.countNonZero(cv2.bitwise_and(jersey, mask)))


def _count_jersey_pixels_half(frame: "np.ndarray", cal: JerseyCalibration, attacks_right: bool) -> int:
    """Count jersey pixels in the attack half of the frame (for mark-only candidates)."""
    h, w = frame.shape[:2]
    if attacks_right:
        region = frame[:, w // 2:, :]
    else:
        region = frame[:, : w // 2, :]
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    jersey = cv2.inRange(hsv, cal.hsv_lower, cal.hsv_upper)
    if cal.hsv_lower2 is not None and cal.hsv_upper2 is not None:
        jersey2 = cv2.inRange(hsv, cal.hsv_lower2, cal.hsv_upper2)
        jersey = cv2.bitwise_or(jersey, jersey2)
    return int(cv2.countNonZero(jersey))


# ── Mark-file cross-reference helpers ────────────────────────────────────────────

_MARK_GOAL_TOL   = 60.0   # seconds — max dist to consider a mark click a goal click
_MARK_SHOT_WIN   = 50.0   # seconds — window to match a TSz candidate to a mark click

def _compute_half_offsets(
    goal_times: list[float],
    mark_times: list[float],
) -> tuple[float, float]:
    """Return (h1_offset, h2_offset) where video_time ≈ mark_time + offset.

    Computed as median(goal_time - nearest_mark_time) per half.
    Falls back to 0 if no matches are found for a half.
    """
    h1_diffs, h2_diffs = [], []
    for gt in goal_times:
        if not mark_times:
            break
        nearest = min(mark_times, key=lambda m: abs(m - gt))
        diff = gt - nearest
        if abs(diff) < _MARK_GOAL_TOL:
            (h1_diffs if gt < 1800 else h2_diffs).append(diff)

    def _median(xs: list[float]) -> float:
        if not xs:
            return 0.0
        s = sorted(xs)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2

    return _median(h1_diffs), _median(h2_diffs)


def _build_xbot_shot_times(
    mark_times: list[float],
    goal_times: list[float],
    h1_offset: float,
    h2_offset: float,
) -> list[float]:
    """Adjust mark times by per-half offset and remove goal-matched clicks."""
    adjusted = [m + (h1_offset if m + h1_offset < 1800 else h2_offset) for m in mark_times]
    result = []
    for adj in adjusted:
        if not any(abs(adj - gt) < _MARK_GOAL_TOL / 2 for gt in goal_times):
            result.append(adj)
    return result


def _match_candidates_to_marks(
    candidates: list[float],
    xbot_shot_times: list[float],
    window: float,
) -> tuple[set[float], set[float]]:
    """Greedy bipartite matching: each mark claims at most one TSz candidate and vice-versa.

    Returns (claimed_candidates, claimed_marks).  Pairs sorted by distance so
    closest matches are assigned first.
    """
    if not xbot_shot_times:
        return set(candidates), set()

    pairs: list[tuple[float, float, float]] = []
    for c in candidates:
        for m in xbot_shot_times:
            d = abs(c - m)
            if d <= window:
                pairs.append((d, c, m))
    pairs.sort()

    claimed_candidates: set[float] = set()
    claimed_marks: set[float] = set()
    for _, c, m in pairs:
        if c not in claimed_candidates and m not in claimed_marks:
            claimed_candidates.add(c)
            claimed_marks.add(m)

    return claimed_candidates, claimed_marks


# ── Main API ─────────────────────────────────────────────────────────────────────

def verify_shots(
    candidates: list[float],
    track_path: Path,
    video_path: Path | None = None,
    goal_times: list[float] | None = None,
    xbot_clips: list[Path] | None = None,
    jersey_color: str = "red",
    h1_attack_side: str | None = None,
    half_offset: float = 0.0,
    mark_times: list[float] | None = None,
    *,
    verbose: bool = False,
) -> list[ShotVerifyResult]:
    """Verify each candidate shot time.

    Pass 1 (always): track-file geometry filters.
    Pass 2 (when video_path is provided): HSV jersey check.
      - Calibration source priority: xbot_clips > goal_times > none
    Pass 3 (when mark_times + goal_times provided): mark-file cross-reference.
      - Computes per-half offset from goal matches, removes goal clicks, then
        rejects any TSz candidate with no nearby non-goal mark click.
    """
    frames = _parse_track_frames(track_path)

    ball_positions: dict[float, tuple[int, int]] = {
        f[0]: (f[2], f[3]) for f in frames
    }

    # Calibrate jersey HSV — prefer XBotGo clips (closer, cleaner signal)
    calibration: JerseyCalibration | None = None
    if video_path and _CV2_AVAILABLE:
        if xbot_clips:
            calibration = calibrate_from_xbot_clips(xbot_clips, jersey_color=jersey_color, verbose=verbose)
        if calibration is None and goal_times:
            calibration = calibrate_jersey_hsv(
                video_path, goal_times, ball_positions, verbose=verbose
            )

    # Pass 3 setup: compute per-half offset, build xbot shot times, pre-match
    mark_verified: set[float] | None = None
    xbot_shot_times: list[float] | None = None
    claimed_marks: set[float] = set()
    if mark_times and goal_times:
        h1_off, h2_off = _compute_half_offsets(goal_times, mark_times)
        xbot_shot_times = _build_xbot_shot_times(mark_times, goal_times, h1_off, h2_off)
        # Pre-match over all raw candidates so mark assignment is stable regardless
        # of which pass-1/2 filters fire.
        mark_verified, claimed_marks = _match_candidates_to_marks(candidates, xbot_shot_times, _MARK_SHOT_WIN)
        if verbose:
            print(f"  [mark-cal] H1 offset={h1_off:+.1f}s  H2 offset={h2_off:+.1f}s")
            print(f"  [mark-cal] {len(xbot_shot_times)} non-goal mark clicks → xbot shot times")
            for xt in sorted(xbot_shot_times):
                print(f"    {int(xt)//60:02d}:{int(xt)%60:02d}  ({xt:.0f}s)")

    results: list[ShotVerifyResult] = []

    for t in candidates:
        angle, by, rb, bx, _, peak_t = _cluster_features(frames, t)

        # Pass 1: geometry
        if rb < _MIN_RB:
            results.append(ShotVerifyResult(t, angle, by, rb, "reject_rb", f"rb={rb}<{_MIN_RB}"))
            continue
        if angle > _MAX_LAUNCH_ANGLE:
            results.append(ShotVerifyResult(t, angle, by, rb, "reject_angle", f"angle={angle:.1f}°"))
            continue
        if not (_MIN_BALL_Y <= by <= _MAX_BALL_Y):
            results.append(ShotVerifyResult(t, angle, by, rb, "reject_y", f"ball_y={by}"))
            continue

        # Pass 2: jersey check — extract frame at the PEAK SPEED frame time so
        # the ball position (bx, by) and the video frame are in sync.
        jersey_px = 0
        if calibration and video_path:
            frame = _extract_frame(video_path, peak_t)
            if frame is not None:
                jersey_px = _count_jersey_pixels(frame, bx, by, calibration)
                if jersey_px < _JERSEY_MIN_PIXELS:
                    note = f"jersey_px={jersey_px}<{_JERSEY_MIN_PIXELS} angle={angle:.1f}° rb={rb}"
                    results.append(ShotVerifyResult(t, angle, by, rb, "reject_jersey", note, jersey_px))
                    if verbose:
                        m, s = int(t // 60), int(t % 60)
                        print(f"  {m:3d}:{s:02d}  reject_jersey  {note}")
                    continue

        # Pass 3: mark-file cross-reference (unique nearest-mark assignment)
        if mark_verified is not None and t not in mark_verified:
            # Compute nearest mark for the rejection note
            nearest_dist = min((abs(t - xt) for xt in xbot_shot_times), default=float("inf"))
            note = f"no unique mark match within {_MARK_SHOT_WIN:.0f}s (nearest={nearest_dist:.0f}s)"
            results.append(ShotVerifyResult(t, angle, by, rb, "reject_mark", note, jersey_px))
            if verbose:
                m, s = int(t // 60), int(t % 60)
                print(f"  {m:3d}:{s:02d}  reject_mark    {note}")
            continue

        note = f"angle={angle:.1f}° rb={rb} by={by} jersey={jersey_px}"
        results.append(ShotVerifyResult(t, angle, by, rb, "verified", note, jersey_px))
        if verbose:
            m, s = int(t // 60), int(t % 60)
            print(f"  {m:3d}:{s:02d}  verified       {note}")

    # ── Mark-only pass: non-goal mark clicks not claimed by any TSz candidate ──────
    # These are shots XBotGo detected that the track file's TSz data never captured.
    # We verify them with a half-frame jersey scan to filter out opponent shots.
    if xbot_shot_times is not None:
        mark_only = [xt for xt in xbot_shot_times if xt not in claimed_marks]
        if mark_only and verbose:
            print(f"  [mark-only] {len(mark_only)} mark click(s) with no TSz candidate")
        for t in sorted(mark_only):
            jersey_px = 0
            if calibration and video_path and _CV2_AVAILABLE:
                frame = _extract_frame(video_path, t)
                if frame is not None:
                    in_h2 = t >= HALFTIME_SECONDS
                    attacks_right = (h1_attack_side == "right") != in_h2 if h1_attack_side else True
                    jersey_px = _count_jersey_pixels_half(frame, calibration, attacks_right)
                    if jersey_px < _JERSEY_MIN_PIXELS_HALF:
                        note = f"mark-only jersey_half={jersey_px}<{_JERSEY_MIN_PIXELS_HALF}"
                        results.append(ShotVerifyResult(t, 0.0, 0, 0, "reject_jersey", note, jersey_px))
                        if verbose:
                            m, s = int(t // 60), int(t % 60)
                            print(f"  {m:3d}:{s:02d}  reject_jersey  {note}")
                        continue
            note = f"mark-only jersey_half={jersey_px}"
            results.append(ShotVerifyResult(t, 0.0, 0, 0, "verified", note, jersey_px))
            if verbose:
                m, s = int(t // 60), int(t % 60)
                print(f"  {m:3d}:{s:02d}  verified(mark) {note}")

    return results


# ── CLI ──────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json, sys

    ap = argparse.ArgumentParser(description="Verify shot-on-target candidates from track file")
    ap.add_argument("--track",      required=True, help="Path to .track file")
    ap.add_argument("--manifest",   help="kickreel.json — reads shotEvents + goal marker_times")
    ap.add_argument("--times",      nargs="+", type=float, help="Candidate shot times (seconds)")
    ap.add_argument("--video",      help="Video .mp4 — enables HSV jersey calibration")
    ap.add_argument("--xbot-clips", nargs="+", help="XBotGo confirmed-shot .MP4 files for jersey calibration")
    ap.add_argument("--jersey-color", choices=["red","blue","orange","yellow","green","purple"], default="red")
    ap.add_argument("--h1-attack-side", choices=["right", "left"], default="right")
    ap.add_argument("--half-offset",    type=float, default=0.0)
    ap.add_argument("--json",       action="store_true")
    args = ap.parse_args()

    goal_times: list[float] = []

    mark_times: list[float] = []

    if args.manifest:
        data = json.loads(Path(args.manifest).read_text())
        vid0 = data["videos"][0]
        times = [e["timeSeconds"] for e in (vid0.get("analytics") or {}).get("shotEvents", [])]
        goal_times = [c["marker_time"] for c in vid0.get("clips", []) if c.get("clip_type") == "goal"]
        mark_times = [m["timeSeconds"] for m in vid0.get("markers", [])]
        # Read stored xbot clips + jersey color from manifest if not overridden on CLI
        if not args.xbot_clips and data.get("xbot_clip_paths"):
            args.xbot_clips = [p for p in data["xbot_clip_paths"] if Path(p).exists()]
        if not hasattr(args, "jersey_color") or not args.jersey_color:
            args.jersey_color = data.get("jersey_color", "red")
    elif args.times:
        times = args.times
    else:
        print("Provide --times or --manifest", file=sys.stderr)
        sys.exit(1)

    xbot_clips = [Path(p) for p in args.xbot_clips] if args.xbot_clips else None
    jersey_color = getattr(args, "jersey_color", None) or "red"

    results = verify_shots(
        times,
        Path(args.track),
        video_path=Path(args.video) if args.video else None,
        goal_times=goal_times or None,
        xbot_clips=xbot_clips,
        jersey_color=jersey_color,
        h1_attack_side=args.h1_attack_side,
        half_offset=args.half_offset,
        mark_times=mark_times or None,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps([
            {"time_seconds": r.time_seconds, "verdict": r.verdict,
             "angle_deg": round(r.angle_deg, 1), "ball_y": r.ball_y,
             "rb": r.rb, "jersey_pixels": r.jersey_pixels, "note": r.note}
            for r in results
        ], indent=2))
    else:
        verified = [r for r in results if r.verdict == "verified"]
        rejected = [r for r in results if r.verdict.startswith("reject")]
        print(f"\n{'─'*60}")
        print(f"Candidates : {len(results)}")
        print(f"Verified   : {len(verified)}")
        print(f"Rejected   : {len(rejected)}")
        print(f"{'─'*60}")
        for r in verified:
            m, s = int(r.time_seconds // 60), int(r.time_seconds % 60)
            print(f"  {m:3d}:{s:02d}  angle={r.angle_deg:.1f}° rb={r.rb} by={r.ball_y}"
                  + (f" jersey={r.jersey_pixels}" if r.jersey_pixels else ""))
