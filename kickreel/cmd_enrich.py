"""Step 3 — Enrich clips with player names from a KickReel Stats tracker JSON."""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from .manifest import load, save


_TRACK_UTC_PAT = re.compile(r"UTC(\d+):(\d+):(\d+\.\d+)")
# Camera operator taps the XBotGo button at the moment they see a play happen;
# the tracker person logs the event on their phone seconds to a few minutes later.
# Allow a 4-minute window and preserve chronological order between events and clips.
_MATCH_WINDOW_S = 240


def _track_t0_hms(track_path: Path) -> tuple[int, int, float] | None:
    with open(track_path, errors="replace") as f:
        for line in f:
            m = _TRACK_UTC_PAT.match(line)
            if m:
                return int(m.group(1)), int(m.group(2)), float(m.group(3))
    return None


def _t0_actual_utc(session_date: str, h: int, m: int, s: float) -> datetime:
    """
    The .track file labels local time as "UTC" (camera firmware bug).
    Re-interpret it as local time and convert to actual UTC using the
    machine's current UTC offset (same timezone as the recording).
    """
    date_part = session_date[:10]           # "2026-10-18"
    year, month, day = int(date_part[:4]), int(date_part[5:7]), int(date_part[8:10])
    from datetime import timezone
    local_naive = datetime(year, month, day, h, m, int(s))
    utc_offset = datetime.now().astimezone().utcoffset()
    return (local_naive - utc_offset).replace(tzinfo=timezone.utc)


def run(
    library: Path,
    tracker_path: Path,
    dry_run: bool = False,
    match_id: str | None = None,
) -> None:
    print(f"\nKickReel enrich{'  [DRY RUN]' if dry_run else ''}\n")

    tracker_data = json.loads(tracker_path.read_text())
    events = tracker_data.get("events", [])
    tracker_date = tracker_data.get("date", "")

    print(f"Tracker  : {tracker_path.name}")
    print(f"Opponent : {tracker_data.get('opponent', '?')}")
    print(f"Events   : {len(events)}")
    print()

    session_dirs = sorted(
        d for d in library.iterdir()
        if d.is_dir() and (d / "kickreel.json").exists()
        and (match_id is None or d.name.endswith(match_id))
    )
    if not session_dirs:
        print("No sessions found.")
        return

    # Auto-filter to sessions whose folder name contains the tracker date
    if match_id is None and tracker_date:
        session_dirs = [d for d in session_dirs if tracker_date in d.name]
        if not session_dirs:
            print(
                f"No session found for date {tracker_date}.\n"
                f"Use --match-id to target a specific session."
            )
            return

    for session_dir in session_dirs:
        manifest = load(session_dir)
        if not manifest:
            continue

        print(f"{'─'*50}")
        print(f"Session  : {manifest.session_name}  [{manifest.session_type}]")

        matched_total = 0

        for video in manifest.videos:
            if not video.track_path:
                print(f"  [{video.file_name}] no track path — skipping")
                continue

            hms = _track_t0_hms(Path(video.track_path))
            if hms is None:
                print(f"  [{video.file_name}] could not read track t0 — skipping")
                continue

            t0_utc = _t0_actual_utc(manifest.session_date, *hms)
            print(f"  [{video.file_name}]  t0 UTC = {t0_utc.strftime('%H:%M:%S')}")

            # Map each tracker event to a video-relative time and check it falls in this video
            video_events: list[tuple[dict, float]] = []
            for ev in events:
                ev_dt = datetime.fromisoformat(ev["wallTimestamp"].replace("Z", "+00:00"))
                ev_video_s = (ev_dt - t0_utc).total_seconds()
                duration = video.duration or 7200
                if -60 <= ev_video_s <= duration + 60:
                    video_events.append((ev, ev_video_s))

            print(f"  Events in this video: {len(video_events)}")

            # Monotone order-preserving matching.
            # Camera taps (clips) and tracker events both arrive chronologically.
            # The camera person reacts immediately; the tracker person logs a bit later.
            # We therefore scan both lists in time order and greedily pair each event
            # with the first available clip within the window, preserving their relative
            # ordering (no crossing matches). This gives correct assignments even when
            # multiple events cluster closely together.
            sorted_events = sorted(video_events, key=lambda x: x[1])
            sorted_clips = sorted(video.clips, key=lambda c: c.marker_time)

            assignments: list[tuple[dict, float, object, float]] = []
            unmatched_event_ids: set[str] = set()
            ei = 0
            ci = 0
            while ei < len(sorted_events) and ci < len(sorted_clips):
                ev, ev_video_s = sorted_events[ei]
                clip = sorted_clips[ci]
                # Positive delta: clip is before event (normal — camera tap precedes tracker log)
                delta = ev_video_s - clip.marker_time
                if delta < -_MATCH_WINDOW_S:
                    # Clip is way after this event; event has no clip
                    unmatched_event_ids.add(ev["id"])
                    ei += 1
                elif delta > _MATCH_WINDOW_S:
                    # Clip is way before this event; skip clip
                    ci += 1
                else:
                    assignments.append((ev, ev_video_s, clip, abs(delta)))
                    ei += 1
                    ci += 1

            # Any events not reached by the clip pointer are unmatched
            for ev, ev_video_s in sorted_events[ei:]:
                unmatched_event_ids.add(ev["id"])

            for ev, ev_video_s in sorted_events:
                if ev["id"] in unmatched_event_ids:
                    nearest_delta = min(
                        (abs(c.marker_time - ev_video_s) for c in video.clips),
                        default=float("inf"),
                    )
                    print(
                        f"    [UNMATCHED]  tracker={int(ev_video_s)}s ({ev.get('minute', '?')}')  "
                        f"{ev.get('playerName', '')}  (nearest clip {nearest_delta:.0f}s away)"
                    )

            for ev, ev_video_s, clip, delta in assignments:
                player = ev.get("playerName", "")
                assist = ev.get("assistPlayerName", "")
                half = ev.get("half", 1)
                tracker_minute = ev.get("minute", 0)

                new_label = f"H{half} {tracker_minute}' — {player}"
                if assist:
                    new_label += f" ({assist})"

                print(
                    f"    [match]  tracker={int(ev_video_s)}s ({tracker_minute}')  "
                    f"clip={clip.marker_time:.0f}s  Δ={delta:.0f}s  → {new_label}"
                )

                if not dry_run:
                    clip.label = new_label
                    clip.clip_type = ev.get("type", "highlight")
                    clip.player_name = player
                    clip.assist_player_name = assist
                matched_total += 1

        if dry_run:
            print(f"\n  [dry-run] no files written")
        elif matched_total > 0:
            save(session_dir, manifest)
            print(f"\n  ✓ {matched_total} clip(s) enriched. Manifest saved.")
        else:
            print(f"\n  No clips matched — manifest unchanged")

    print(f"\n{'─'*50}")
    print("Enrichment complete.\n")
