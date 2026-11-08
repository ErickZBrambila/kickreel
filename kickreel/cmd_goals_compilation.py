"""Step 4b — Build a goals-compilation MP4 from enriched goal clips.

Pipeline:
  1. Render scoreboard (inline Pillow → ffmpeg composite onto template)
  2. Normalize each goal clip → H.264 1080p 25fps
  3. Concat: intro + scoreboard + normalized goals
  4. Write compilation_path into manifest (picked up by 'publish')
"""

import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .manifest import load, save


_LIBRARY_TEMPLATES = Path.home() / "Movies" / "KickReel"
_SCOREBOARD_TEMPLATE = _LIBRARY_TEMPLATES / "north_legends_score_template_1080p25.mp4"
_INTRO = _LIBRARY_TEMPLATES / "north_legends_intro_1080p25.mp4"

_NORMALIZE_FLAGS = [
    "-r", "25",
    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-video_track_timescale", "25000",
    "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
]


def _render_scoreboard(
    home_score: int,
    away_team: str,
    away_score: int,
    match_date: str,
    output: Path,
) -> bool:
    overlay_png = output.parent / ".score_overlay_tmp.png"
    py_code = f"""
from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 1080
img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
draw = ImageDraw.Draw(img)

WHITE = (240, 242, 246, 255)
GOLD  = (212, 168, 60,  255)
GREY  = (100, 110, 130, 255)
DARK  = (21,  27,  38,  255)
DARK2 = (18,  22,  31,  255)
DARK3 = (23,  28,  38,  255)

def _font(size, bold=True):
    paths = [
        "/System/Library/Fonts/Supplemental/Impact.ttf" if bold else None,
        "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else None,
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for p in paths:
        if p:
            try: return ImageFont.truetype(p, size)
            except: pass
    return ImageFont.load_default()

score_font  = _font(72)
away_font   = _font(65)
bar_font    = _font(20, bold=False)

def centred(text, cx, cy, font, color):
    bb = draw.textbbox((0,0), text, font=font)
    draw.text((cx-(bb[2]-bb[0])//2, cy-(bb[3]-bb[1])//2), text, font=font, fill=color)

def left(text, x, cy, font, color):
    bb = draw.textbbox((0,0), text, font=font)
    draw.text((x, cy-(bb[3]-bb[1])//2), text, font=font, fill=color)

draw.rectangle([(765, 578), (1100, 632)], fill=DARK)
draw.rectangle([(1744, 396), (1778, 406)], fill=DARK2)
draw.rectangle([(1744, 602), (1778, 612)], fill=DARK2)
draw.rectangle([(626, 780), (1858, 840)], fill=DARK3)

centred("{home_score}", 1761, 387, score_font, WHITE)
centred("{away_score}", 1761, 592, score_font, WHITE)
left("{away_team.upper()}", 774, 602, away_font, WHITE)
centred("FINAL SCORE", 1240, 802, bar_font, GOLD)
left("{match_date}", 660, 802, bar_font, GREY)

img.save(r"{overlay_png}", "PNG")
"""
    r = subprocess.run(["python3", "-c", py_code], capture_output=True)
    if r.returncode != 0:
        print(f"  Scoreboard overlay failed: {r.stderr.decode()[:200]}")
        return False

    r = subprocess.run([
        "ffmpeg", "-y",
        "-i", str(_SCOREBOARD_TEMPLATE),
        "-i", str(overlay_png),
        "-filter_complex", "[1:v]format=rgba[ovr];[0:v][ovr]overlay=0:0[vout]",
        "-map", "[vout]", "-map", "0:a",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-profile:v", "high", "-level:v", "4.1",
        "-video_track_timescale", "25000",
        "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "128k",
        str(output),
    ], capture_output=True)
    overlay_png.unlink(missing_ok=True)
    return r.returncode == 0


def _normalize(src: Path, dst: Path) -> bool:
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", str(src)] + _NORMALIZE_FLAGS + [str(dst)],
        capture_output=True,
    )
    return r.returncode == 0


def run(
    library: Path,
    match_id: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> None:
    print(f"\nKickReel goals-compilation{'  [DRY RUN]' if dry_run else ''}: scanning {library}\n")

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
    )

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        if manifest.session_type != "GAME":
            continue

        if manifest.compilation_path and not force:
            print(f"[skip] {manifest.session_name} — compilation already exists (use --force to rebuild)")
            continue

        goal_clips = [
            (session_dir / "clips" / c.file_name, c)
            for v in manifest.videos
            for c in sorted(v.clips, key=lambda x: x.marker_time)
            if c.clip_type == "goal"
        ]

        if not goal_clips:
            print(f"[skip] {manifest.session_name} — no goal clips (run enrich first)")
            continue

        date_str = manifest.session_date[:10]
        try:
            dt = datetime.fromisoformat(date_str)
            label_date = dt.strftime("%b %-d, %Y").upper()
            file_date  = dt.strftime("%b%d").upper()
        except ValueError:
            label_date = date_str
            file_date  = date_str.replace("-", "")

        away = manifest.away_team if manifest.home_away == "home" else manifest.home_team
        away_safe = away.replace(" ", "").replace("/", "")
        home_score = manifest.goals_for if manifest.goals_for >= 0 else 0
        away_score = manifest.goals_against if manifest.goals_against >= 0 else 0

        goals_dir = session_dir / "goals"
        norm_dir  = goals_dir / "normalized"
        out_name  = f"NorthLegends_vs_{away_safe}_{file_date}_Goals.mp4"
        out_path  = goals_dir / out_name

        print(f"{'─'*50}")
        print(f"Session  : {manifest.session_name}  [{date_str}]")
        print(f"Score    : {home_score}–{away_score} vs {away}")
        print(f"Goals    : {len(goal_clips)}")
        for src, c in goal_clips:
            print(f"  {c.label}  ({src.name})")

        if dry_run:
            print(f"\n  [dry-run] would write → {out_path}")
            continue

        goals_dir.mkdir(exist_ok=True)
        norm_dir.mkdir(exist_ok=True)

        # 1. Render scoreboard
        scoreboard = goals_dir / "scoreboard.mp4"
        print(f"\n  Rendering scoreboard…")
        if not _render_scoreboard(home_score, away, away_score, label_date, scoreboard):
            print("  ✗ Scoreboard render failed — aborting")
            continue
        print(f"  ✓ scoreboard.mp4")

        # 2. Normalize goal clips
        normalized: list[Path] = []
        for i, (src, c) in enumerate(goal_clips, 1):
            dst = norm_dir / f"{i:02d}_{src.stem}.mp4"
            print(f"  Normalizing [{i}/{len(goal_clips)}] {src.name}…")
            if not _normalize(src, dst):
                print(f"  ✗ normalize failed for {src.name} — skipping")
                continue
            normalized.append(dst)
        print(f"  ✓ {len(normalized)} clips normalized")

        if not normalized:
            print("  No clips normalized — aborting")
            continue

        # 3. Concat: intro + scoreboard + goal clips
        concat_list = goals_dir / "concat_list.txt"
        entries: list[Path] = []
        if _INTRO.exists():
            entries.append(_INTRO)
        entries.append(scoreboard)
        entries.extend(normalized)

        concat_list.write_text(
            "\n".join(f"file '{p}'" for p in entries) + "\n"
        )

        print(f"  Concatenating {len(entries)} segments…")
        r = subprocess.run([
            "ffmpeg", "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy",
            str(out_path),
        ], capture_output=True)

        if r.returncode != 0:
            print(f"  ✗ concat failed: {r.stderr.decode()[-300:]}")
            continue

        print(f"  ✓ {out_name}")

        manifest.compilation_path = str(out_path)
        save(session_dir, manifest)
        print(f"  ✓ manifest.compilation_path updated")

    print(f"\n{'─'*50}")
    print("Goals compilation complete.\n")
