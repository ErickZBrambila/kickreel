"""Per-session manifest — tracks state across offload → process → publish."""

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


MANIFEST_NAME = "kickreel.json"


@dataclass
class ClipRecord:
    file_name: str
    start_seconds: float
    end_seconds: float
    marker_time: float
    label: str
    r2_url: str = ""
    clip_type: str = ""  # "highlight" | "goal" | "rival_goal" | "celebration" — set manually or via pipeline
    player_name: str = ""
    assist_player_name: str = ""


@dataclass
class VideoRecord:
    file_name: str
    local_path: str
    cover_path: str
    duration: float
    resolution: str
    track_path: str = ""
    vdata_path: str = ""
    mark_path: str = ""
    drive_file_id: str = ""
    drive_url: str = ""
    half_offset_seconds: float = 0.0  # game-clock offset at recording start (e.g. 2700 = 2nd half)
    markers: list[dict] = field(default_factory=list)
    clips: list[ClipRecord] = field(default_factory=list)
    analytics: dict = field(default_factory=dict)


@dataclass
class SessionManifest:
    match_id: str
    session_name: str
    session_type: str
    session_date: str
    home_team: str
    away_team: str
    offloaded: bool = False
    processed: bool = False
    compiled: bool = False   # shot clips cut from analytics
    published: bool = False
    sanity_doc_id: str = ""
    compilation_path: str = ""  # local path to the compiled goals MP4; picked up by 'publish'
    # "right" = team attacks outx>1700 side in H1, flips in H2; "left" = opposite; None = detect both
    h1_attack_side: str | None = None
    xbot_clip_paths: list[str] = field(default_factory=list)  # XBotGo confirmed-shot clips for jersey HSV calibration
    jersey_color: str = "red"  # team jersey color hint for HSV calibration: red|blue|orange|yellow|green|purple
    videos: list[VideoRecord] = field(default_factory=list)


_VIDEO_FIELDS = {f.name for f in VideoRecord.__dataclass_fields__.values()}  # type: ignore[attr-defined]
_SESSION_FIELDS = {f.name for f in SessionManifest.__dataclass_fields__.values()}  # type: ignore[attr-defined]


def load(session_dir: Path) -> SessionManifest | None:
    p = session_dir / MANIFEST_NAME
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    videos = [
        VideoRecord(
            **{k: v for k, v in vd.items() if k in _VIDEO_FIELDS and k not in ("clips", "analytics")},
            clips=[ClipRecord(**c) for c in vd.get("clips", [])],
            analytics=vd.get("analytics", {}),
        )
        for vd in data.pop("videos", [])
    ]
    return SessionManifest(**{k: v for k, v in data.items() if k in _SESSION_FIELDS}, videos=videos)


def save(session_dir: Path, manifest: SessionManifest) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    data = asdict(manifest)
    (session_dir / MANIFEST_NAME).write_text(json.dumps(data, indent=2))
