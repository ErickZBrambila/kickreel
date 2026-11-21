import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MarkerEvent:
    type: str
    time_seconds: float


@dataclass
class VideoMeta:
    file_name: str
    file_path: Path
    duration: float
    fps: float
    width: int
    height: int
    bit_rate: int
    has_mark: bool
    match_id: str
    markers: list[MarkerEvent]
    cover_path: Path | None
    track_path: Path | None = None
    vdata_path: Path | None = None
    mark_path: Path | None = None

    @property
    def resolution(self) -> str:
        return "4K" if self.width >= 3840 else "1080p"


@dataclass
class MatchSession:
    match_id: str
    session_name: str
    session_type: str
    match_start: str
    match_end: str
    home_team: str
    away_team: str
    timezone: str
    videos: list[VideoMeta] = field(default_factory=list)


def load_sessions(source_dir: Path) -> list[MatchSession]:
    sessions: dict[str, MatchSession] = {}

    match_dir = source_dir / ".data" / "match"
    if match_dir.exists():
        for match_file in sorted(match_dir.glob("*.match")):
            try:
                data = json.loads(match_file.read_text().rstrip("\x00"))
                info = data["MatchInfo"]
                match_id = str(int(float(info["MatchId"])))
                sessions[match_id] = MatchSession(
                    match_id=match_id,
                    session_name=info.get("MatchName", "Practice"),
                    session_type=info.get("MatchType", "PRACTICE"),
                    match_start=info.get("MatchStart", ""),
                    match_end=info.get("MatchEnd", ""),
                    home_team=data.get("HomeInfo", {}).get("TeamName", "Home"),
                    away_team=data.get("AwayInfo", {}).get("TeamName", "Away"),
                    timezone=info.get("TimeZone", "UTC"),
                )
            except (json.JSONDecodeError, KeyError, ValueError) as e:
                print(f"  Warning: couldn't parse {match_file.name}: {e}")

    for date_dir in sorted(source_dir.iterdir()):
        if not date_dir.is_dir() or date_dir.name.startswith("."):
            continue
        data_dir = date_dir / ".data"

        for mp4 in sorted(date_dir.glob("*.mp4")):
            vdata_path = data_dir / mp4.with_suffix(".vdata").name
            if not vdata_path.exists():
                continue

            try:
                vdata = json.loads(vdata_path.read_text())
            except (json.JSONDecodeError, FileNotFoundError):
                continue

            raw_match_id = vdata.get("match_id", 0)
            try:
                match_id = str(int(float(raw_match_id)))
            except (ValueError, TypeError):
                match_id = str(raw_match_id)

            markers: list[MarkerEvent] = []
            mark_path = data_dir / mp4.with_suffix(".mark").name
            if mark_path.exists():
                try:
                    for e in json.loads(mark_path.read_text()):
                        markers.append(MarkerEvent(type=e["type"], time_seconds=float(e["time"])))
                except (json.JSONDecodeError, KeyError):
                    pass
            mark_path_or_none = mark_path if mark_path.exists() else None

            cover_path = data_dir / mp4.with_suffix(".cover").name
            track_path = data_dir / mp4.with_suffix(".track").name

            # Parse [SoccerEvent] Submit entries from track file (fw 5.1+ auto-detection)
            _SOCCER_TYPE_MAP = {"Goal": "goal", "CornerKick": "corner", "PenaltyKick": "penalty"}
            _frame_re = re.compile(r"UTC\S+ fps(\d+) Frame(\d+)")
            _event_re = re.compile(r"\[SoccerEvent\] Submit type=(\w+)")
            if track_path.exists():
                try:
                    track_lines = track_path.read_text(errors="replace").splitlines()
                    # Determine start frame from first header line
                    start_frame, fps_track = 0, 20
                    m0 = _frame_re.match(track_lines[0]) if track_lines else None
                    if m0:
                        fps_track, start_frame = int(m0.group(1)), int(m0.group(2))
                    for idx, line in enumerate(track_lines):
                        em = _event_re.search(line)
                        if not em:
                            continue
                        ev_type = _SOCCER_TYPE_MAP.get(em.group(1))
                        if ev_type is None:
                            continue
                        # find nearest previous frame header for timestamp
                        for j in range(idx, max(0, idx - 20), -1):
                            fm = _frame_re.match(track_lines[j])
                            if fm:
                                secs = (int(fm.group(2)) - start_frame) / fps_track
                                markers.append(MarkerEvent(type=ev_type, time_seconds=round(secs, 2)))
                                break
                except Exception:
                    pass

            video = VideoMeta(
                file_name=mp4.name,
                file_path=mp4,
                duration=float(vdata.get("duration", 0)),
                fps=float(vdata.get("fps", 30)),
                width=int(vdata.get("width", 1920)),
                height=int(vdata.get("height", 1080)),
                bit_rate=int(vdata.get("bit_rate", 0)),
                has_mark=bool(vdata.get("has_mark", 0)),
                match_id=match_id,
                markers=markers,
                cover_path=cover_path if cover_path.exists() else None,
                track_path=track_path if track_path.exists() else None,
                vdata_path=vdata_path if vdata_path.exists() else None,
                mark_path=mark_path_or_none,
            )

            if match_id in sessions:
                sessions[match_id].videos.append(video)
            else:
                file_time = vdata.get("file_time", mp4.stem)
                sessions[match_id] = MatchSession(
                    match_id=match_id,
                    session_name=f"Recording {str(file_time)[:10]}",
                    session_type="PRACTICE",
                    match_start=str(file_time),
                    match_end="",
                    home_team="",
                    away_team="",
                    timezone="UTC",
                    videos=[video],
                )

    return list(sessions.values())
