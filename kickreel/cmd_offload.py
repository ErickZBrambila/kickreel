"""Step 1 — Copy SD card to local storage and write manifests."""

import shutil
from pathlib import Path

from .ingest import load_sessions
from .manifest import SessionManifest, VideoRecord, save


def run(source: Path, dest: Path) -> None:
    print(f"\nKickReel offload: {source} → {dest}\n")

    sessions = load_sessions(source)
    if not sessions:
        print("No sessions found on SD card.")
        return

    print(f"Found {len(sessions)} session(s)\n")

    for session in sessions:
        date_label = session.match_start[:10] if session.match_start else "unknown"
        session_dir = dest / f"{date_label}_{session.match_id}"

        print(f"{'─'*50}")
        print(f"Session : {session.session_name}  [{session.session_type}]  {date_label}")
        print(f"Videos  : {len(session.videos)}")

        video_records: list[VideoRecord] = []

        for video in session.videos:
            size_gb = video.file_path.stat().st_size / 1e9
            video_dest = session_dir / "footage" / video.file_name
            video_dest.parent.mkdir(parents=True, exist_ok=True)

            print(f"\n  Copying {video.file_name}  ({size_gb:.1f} GB)…")
            shutil.copy2(video.file_path, video_dest)
            print(f"  ✓ video copied")

            raw_dir = session_dir / "raw_data"
            raw_dir.mkdir(parents=True, exist_ok=True)

            cover_dest_str = ""
            if video.cover_path:
                cover_dest = session_dir / "thumbs" / video.cover_path.name
                cover_dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(video.cover_path, cover_dest)
                cover_dest_str = str(cover_dest)

            track_dest_str = ""
            if video.track_path:
                track_dest = raw_dir / video.track_path.name
                shutil.copy2(video.track_path, track_dest)
                track_dest_str = str(track_dest)
                print(f"  ✓ track data copied  ({video.track_path.stat().st_size / 1e6:.1f} MB)")

            vdata_dest_str = ""
            if video.vdata_path:
                vdata_dest = raw_dir / video.vdata_path.name
                shutil.copy2(video.vdata_path, vdata_dest)
                vdata_dest_str = str(vdata_dest)

            mark_dest_str = ""
            if video.mark_path:
                mark_dest = raw_dir / video.mark_path.name
                shutil.copy2(video.mark_path, mark_dest)
                mark_dest_str = str(mark_dest)

            click_markers = [
                {"timeSeconds": m.time_seconds, "label": f"{int(m.time_seconds//60)}:{int(m.time_seconds%60):02d}"}
                for m in video.markers if m.type == "click"
            ]

            video_records.append(VideoRecord(
                file_name=video.file_name,
                local_path=str(video_dest),
                cover_path=cover_dest_str,
                duration=video.duration,
                resolution=video.resolution,
                track_path=track_dest_str,
                vdata_path=vdata_dest_str,
                mark_path=mark_dest_str,
                markers=click_markers,
            ))

        session_date = session.match_start.replace(" ", "T") if session.match_start else ""
        manifest = SessionManifest(
            match_id=session.match_id,
            session_name=session.session_name,
            session_type=session.session_type,
            session_date=session_date,
            home_team=session.home_team,
            away_team=session.away_team,
            offloaded=True,
            videos=video_records,
        )
        save(session_dir, manifest)
        print(f"\n  ✓ Manifest written → {session_dir / 'kickreel.json'}")

    print(f"\n{'─'*50}")
    print("Offload complete — SD card is free to reuse.\n")
