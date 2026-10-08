#!/usr/bin/env python3
"""
KickReel — three decoupled steps:

  offload   Copy SD card to local library (free the card immediately)
  process   Extract highlight clips from local footage
  publish   Upload clips to R2 + push metadata to Sanity

Examples:
  python pipeline.py offload  --source /Volumes/Untitled/falcon --library ~/Movies/KickReel
  python pipeline.py process  --library ~/Movies/KickReel
  python pipeline.py publish  --library ~/Movies/KickReel --team u11
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

    # publish
    p_pub = sub.add_parser("publish", help="Upload clips to R2 and push to Sanity")
    p_pub.add_argument("--library", required=True, help="Local library folder")
    p_pub.add_argument("--team", choices=["u11", "u12"], default="u11")

    args = parser.parse_args()

    if args.cmd == "offload":
        from kickreel.cmd_offload import run
        run(
            source=Path(args.source).expanduser(),
            dest=Path(args.library).expanduser(),
        )

    elif args.cmd == "process":
        from kickreel.cmd_process import run
        run(
            library=Path(args.library).expanduser(),
            pad=args.pad,
        )

    elif args.cmd == "publish":
        from kickreel import config  # noqa: loads .env
        from kickreel.cmd_publish import run
        run(
            library=Path(args.library).expanduser(),
            team=args.team,
        )


if __name__ == "__main__":
    main()
