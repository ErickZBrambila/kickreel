#!/usr/bin/env python3
"""
KickReel — decoupled pipeline steps:

  offload       Copy SD card to local library (free the card immediately)
  process       Extract highlight clips from local footage
  analyze       Parse .track files into field heatmap + activity analytics
  cv-analyze    YOLOv8 ball-detection shot analysis (complements 'analyze')
  compile       Cut video clips for shot events detected by 'analyze'
  enrich        Tag clips with player names from a KickReel Stats tracker JSON
  publish       Upload clips to R2 + push metadata to Sanity
  drive-upload  Upload full game videos to Google Drive (resumable, 18 GB+)

Examples:
  python pipeline.py offload       --source /Volumes/Untitled/falcon --library ~/Movies/KickReel
  python pipeline.py process       --library ~/Movies/KickReel
  python pipeline.py analyze       --library ~/Movies/KickReel
  python pipeline.py compile       --library ~/Movies/KickReel
  python pipeline.py enrich        --library ~/Movies/KickReel --tracker ~/Downloads/kickreel-*.json
  python pipeline.py publish       --library ~/Movies/KickReel --team u11
  python pipeline.py drive-upload  --library ~/Movies/KickReel
  python pipeline.py drive-upload  --library ~/Movies/KickReel --dry-run
  python pipeline.py drive-upload  --auth-only
"""

import argparse
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="KickReel pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # offload
    p_off = sub.add_parser("offload", help="Copy SD card to local library")
    p_off.add_argument("--source", required=True, help="Falcon SD card path (e.g. /Volumes/Untitled/falcon)")
    p_off.add_argument("--library", required=True, help="Local library folder (e.g. ~/Movies/KickReel)")

    # process
    p_proc = sub.add_parser("process", help="Extract highlight clips from offloaded footage")
    p_proc.add_argument("--library", required=True, help="Local library folder")
    p_proc.add_argument("--pad", type=int, default=15, help="Seconds before/after each marker (default: 15)")
    p_proc.add_argument("--dry-run", action="store_true", help="Show what clips would be extracted without running ffmpeg")
    p_proc.add_argument("--remove-watermark", action="store_true", help="Blur the XBotGo watermark (slower — requires re-encoding)")
    p_proc.add_argument("--match-id", help="Only process this match ID")

    # compile
    p_cmp = sub.add_parser("compile", help="Cut shot clips from .track analysis results")
    p_cmp.add_argument("--library", required=True, help="Local library folder")
    p_cmp.add_argument("--dry-run", action="store_true", help="Show what clips would be cut without running ffmpeg")
    p_cmp.add_argument("--match-id", help="Only compile this match ID")

    # enrich
    p_enr = sub.add_parser("enrich", help="Tag clips with player names from a tracker JSON")
    p_enr.add_argument("--library", required=True, help="Local library folder")
    p_enr.add_argument("--tracker", required=True, help="Path to KickReel Stats tracker JSON")
    p_enr.add_argument("--dry-run", action="store_true", help="Show matches without writing")
    p_enr.add_argument("--match-id", help="Only enrich this match ID")

    # analyze
    p_ana = sub.add_parser("analyze", help="Parse .track files and store analytics in manifests")
    p_ana.add_argument("--library", required=True, help="Local library folder")
    p_ana.add_argument("--dry-run", action="store_true", help="Show analytics without writing to manifests")
    p_ana.add_argument("--match-id", help="Only analyze this match ID")
    p_ana.add_argument("--attack-direction", choices=["right", "left"], help="H1 attack direction (right=outx>1700 goal). Stored in manifest and used to filter Ball TSz shots to our team only.")
    p_ana.add_argument("--xbot-clips", nargs="+", help="XBotGo confirmed-shot .MP4 files for jersey HSV calibration. Stored in manifest; only needed once per session.")
    p_ana.add_argument("--jersey-color", choices=["red","blue","orange","yellow","green","purple"], default=None, help="Team jersey color hint for HSV calibration (default: red). Stored in manifest.")

    # cv-analyze
    p_cv = sub.add_parser("cv-analyze", help="YOLOv8 ball-detection shot analysis (complements 'analyze')")
    p_cv.add_argument("--library", required=True, help="Local library folder")
    p_cv.add_argument("--dry-run", action="store_true", help="Show shots without writing to manifests")
    p_cv.add_argument("--match-id", help="Only analyze this match ID")
    p_cv.add_argument("--attack-direction", choices=["right", "left"], help="H1 attack direction")
    p_cv.add_argument("--xbot-clips", nargs="+", help="XBotGo confirmed-shot clips for jersey calibration")
    p_cv.add_argument("--jersey-color", choices=["red","blue","orange","yellow","green","purple"], default=None)

    # publish
    p_pub = sub.add_parser("publish", help="Upload clips to R2 and push to Sanity")
    p_pub.add_argument("--library", required=True, help="Local library folder")
    p_pub.add_argument("--team", choices=["u11", "u12"], default="u11")
    p_pub.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without touching R2 or Sanity")
    p_pub.add_argument("--match-id", help="Only publish this match ID")
    p_pub.add_argument("--compilation", help="Path to goals compilation MP4 — uploaded to R2 and wired as main session video + ribbon")

    # drive-upload
    p_drive = sub.add_parser("drive-upload", help="Upload full game videos to Google Drive")
    p_drive.add_argument("--library", help="Local library folder")
    p_drive.add_argument("--dry-run", action="store_true", help="Show what would be uploaded without uploading")
    p_drive.add_argument("--auth-only", action="store_true", help="Run OAuth flow and exit (no upload)")
    p_drive.add_argument("--match-id", help="Only upload this match ID")

    args = parser.parse_args()

    if args.cmd == "compile":
        from kickreel.cmd_compile import run
        run(
            library=Path(args.library).expanduser(),
            dry_run=args.dry_run,
            match_id=args.match_id,
        )

    elif args.cmd == "enrich":
        from kickreel.cmd_enrich import run
        run(
            library=Path(args.library).expanduser(),
            tracker_path=Path(args.tracker).expanduser(),
            dry_run=args.dry_run,
            match_id=args.match_id,
        )

    elif args.cmd == "offload":
        from kickreel.cmd_offload import run
        run(
            source=Path(args.source).expanduser(),
            dest=Path(args.library).expanduser(),
        )

    elif args.cmd == "analyze":
        from kickreel.cmd_analyze import run
        xbot_clips = [Path(p).expanduser() for p in args.xbot_clips] if getattr(args, "xbot_clips", None) else None
        run(
            library=Path(args.library).expanduser(),
            dry_run=args.dry_run,
            match_id=args.match_id,
            attack_direction=getattr(args, "attack_direction", None),
            xbot_clips=xbot_clips,
            jersey_color=getattr(args, "jersey_color", None),
        )

    elif args.cmd == "cv-analyze":
        from kickreel.cmd_cv_analyze import run
        xbot_clips = [Path(p).expanduser() for p in args.xbot_clips] if getattr(args, "xbot_clips", None) else None
        run(
            library=Path(args.library).expanduser(),
            dry_run=args.dry_run,
            match_id=args.match_id,
            attack_direction=getattr(args, "attack_direction", None),
            xbot_clips=xbot_clips,
            jersey_color=getattr(args, "jersey_color", None),
        )

    elif args.cmd == "process":
        from kickreel.cmd_process import run
        run(
            library=Path(args.library).expanduser(),
            pad=args.pad,
            dry_run=args.dry_run,
            remove_watermark=args.remove_watermark,
            match_id=args.match_id,
        )

    elif args.cmd == "publish":
        from kickreel import config  # noqa: loads .env
        from kickreel.cmd_publish import run
        run(
            library=Path(args.library).expanduser(),
            team=args.team,
            dry_run=args.dry_run,
            match_id=args.match_id,
            compilation_path=Path(args.compilation).expanduser() if args.compilation else None,
        )

    elif args.cmd == "drive-upload":
        from kickreel.drive import get_service
        if args.auth_only:
            print("Running Google OAuth flow …")
            get_service()
            print("✓ Authenticated. Token saved.")
        else:
            if not args.library:
                parser.error("drive-upload requires --library (or --auth-only)")
            from kickreel.cmd_drive_upload import run
            run(
                library=Path(args.library).expanduser(),
                dry_run=args.dry_run,
                match_id=args.match_id,
            )


if __name__ == "__main__":
    main()
