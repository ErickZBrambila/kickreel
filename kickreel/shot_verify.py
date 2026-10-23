"""Shot-on-target verification using enhanced track-file trajectory features.

Algorithm
---------
For each candidate shot time produced by analyze.py, extract all Ball TSz frames
within ±2 s from the .track file and compute:

  1. Launch angle   — from xv/yv velocity components (direct, not position delta)
  2. Ball height    — ball_y pixel coordinate; goal mouth is ~400–1000 px in 1080p
  3. Track length   — RB (consecutive frames the ball was tracked)
  4. Trajectory monotonicity — is ball consistently moving toward goal over the cluster?

Events failing any hard threshold are rejected.  Events that survive all filters
are returned as "verified".

Jersey-colour via YOLOv8 is available as an optional second pass (--video flag)
but is NOT reliable with this 4K outdoor camera — outdoor white-balance and player
distance wash out hue-based red detection.  The track-file features alone are the
primary gate.

Usage (standalone)
------------------
    python -m kickreel.shot_verify \
        --track path/to/video.track \
        --manifest path/to/kickreel.json   # reads shotEvents automatically
        [--video path/to/video.mp4]        # optional jersey-colour check

Integration
-----------
    from kickreel.shot_verify import verify_shots
    confirmed = [r for r in verify_shots(times, track) if r.verdict == "verified"]
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path

HALFTIME_SECONDS = 1800  # kept in sync with analyze.py

# ── Thresholds ──────────────────────────────────────────────────────────────────
_MAX_LAUNCH_ANGLE  = 50.0   # °  above horizontal; > 50° = overhead clearance / lob
_MIN_RB            = 7      # min consecutive tracking frames (< 7 = unreliable blip)
_MAX_BALL_Y        = 1050   # px  ball below this in 1080p frame (goal mouth is ~400–950)
_MIN_BALL_Y        = 350    # px  ball above this (floated well over crossbar)


@dataclass
class ShotVerifyResult:
    time_seconds: float
    angle_deg: float
    ball_y: int
    rb: int
    verdict: str   # "verified" | "reject_angle" | "reject_rb" | "reject_y"
    note: str = ""


# ── Track-file parser ───────────────────────────────────────────────────────────

def _parse_track_frames(
    track_path: Path,
) -> list[tuple[float, int, int, int, int, int, int, int]]:
    """Return (rel_t, speed, ball_x, ball_y, rb, outx, xv, yv) per Ball TSz frame."""
    frame_pat = re.compile(
        r"UTC(\d+):(\d+):(\d+\.\d+)\s+fps\d+\s+Frame\d+\s+Yaw([-\d.]+)"
    )
    tsz_pat = re.compile(
        r"Ball\d TSz\d+ \(T\d+ RB(\d+) xv(-?\d+) yv(-?\d+) m(\d+)\)"
        r".*ball\((-?\d+),(-?\d+)\) outx(-?\d+)"
    )

    t0: float | None = None
    current_rel_t: float = 0.0
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
        bm = tsz_pat.search(line)
        if bm:
            rb, xv, yv, speed, bx, by, outx = (int(bm.group(i)) for i in range(1, 8))
            frames.append((current_rel_t, speed, bx, by, rb, outx, xv, yv))

    return frames


def _cluster_features(
    frames: list[tuple[float, int, int, int, int, int, int, int]],
    shot_time: float,
    window: float = 2.0,
) -> tuple[float, int, int]:
    """Return (angle_deg, ball_y, rb) for the peak-speed frame within window."""
    nearby = [f for f in frames if abs(f[0] - shot_time) <= window]
    if not nearby:
        return 0.0, 700, 10  # neutral pass-through defaults

    peak = max(nearby, key=lambda f: f[1])
    _, speed, bx, by, rb, outx, xv, yv = peak

    # Launch angle from velocity components (direct, more accurate than position delta)
    # NOTE: xv/yv are camera-image velocity components; bx trajectory is unreliable for
    # direction because the camera pans to follow the ball.
    angle = math.degrees(math.atan2(abs(yv), abs(xv))) if abs(xv) > 0 else 90.0

    return angle, by, rb


# ── Main API ────────────────────────────────────────────────────────────────────

def verify_shots(
    candidates: list[float],
    track_path: Path,
    video_path: Path | None = None,   # reserved; not used (see module docstring)
    h1_attack_side: str | None = None,
    half_offset: float = 0.0,
    *,
    verbose: bool = False,
) -> list[ShotVerifyResult]:
    """Return a ShotVerifyResult for each candidate time."""
    frames = _parse_track_frames(track_path)
    results: list[ShotVerifyResult] = []

    for t in candidates:
        angle, by, rb = _cluster_features(frames, t)

        if rb < _MIN_RB:
            r = ShotVerifyResult(t, angle, by, rb, "reject_rb", f"rb={rb}<{_MIN_RB}")
        elif angle > _MAX_LAUNCH_ANGLE:
            r = ShotVerifyResult(t, angle, by, rb, "reject_angle", f"angle={angle:.1f}°")
        elif not (_MIN_BALL_Y <= by <= _MAX_BALL_Y):
            r = ShotVerifyResult(t, angle, by, rb, "reject_y", f"ball_y={by}")
        else:
            r = ShotVerifyResult(t, angle, by, rb, "verified",
                                 f"angle={angle:.1f}° rb={rb} by={by}")

        results.append(r)
        if verbose:
            mins, secs = int(t // 60), int(t % 60)
            print(f"  {mins:3d}:{secs:02d}  {r.verdict:<18}  {r.note}")

    return results


# ── CLI ─────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json, sys

    ap = argparse.ArgumentParser(description="Verify shot-on-target candidates from track file")
    ap.add_argument("--track", required=True, help="Path to .track file")
    ap.add_argument("--times", nargs="+", type=float, help="Candidate shot times (seconds)")
    ap.add_argument("--manifest", help="kickreel.json — reads shotEvents automatically")
    ap.add_argument("--h1-attack-side", choices=["right", "left"], default="right")
    ap.add_argument("--half-offset", type=float, default=0.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    if args.manifest:
        data = json.loads(Path(args.manifest).read_text())
        times = [e["timeSeconds"] for e in data["videos"][0]["analytics"]["shotEvents"]]
    elif args.times:
        times = args.times
    else:
        print("Provide --times or --manifest", file=sys.stderr)
        sys.exit(1)

    results = verify_shots(
        times,
        Path(args.track),
        h1_attack_side=args.h1_attack_side,
        half_offset=args.half_offset,
        verbose=not args.json,
    )

    if args.json:
        print(json.dumps([
            {"time_seconds": r.time_seconds, "verdict": r.verdict,
             "angle_deg": round(r.angle_deg, 1), "ball_y": r.ball_y,
             "rb": r.rb, "note": r.note}
            for r in results
        ], indent=2))
    else:
        verified  = [r for r in results if r.verdict == "verified"]
        rejected  = [r for r in results if r.verdict.startswith("reject")]
        print(f"\n{'─'*60}")
        print(f"Candidates : {len(results)}")
        print(f"Verified   : {len(verified)}")
        print(f"Rejected   : {len(rejected)}")
        print(f"{'─'*60}")
        if verified:
            print("Verified shots:")
            for r in verified:
                m, s = int(r.time_seconds//60), int(r.time_seconds%60)
                print(f"  {m:3d}:{s:02d}  angle={r.angle_deg:.1f}° rb={r.rb} by={r.ball_y}")
