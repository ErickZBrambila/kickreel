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
    published: bool = False
    sanity_doc_id: str = ""
    videos: list[VideoRecord] = field(default_factory=list)


def load(session_dir: Path) -> SessionManifest | None:
    p = session_dir / MANIFEST_NAME
    if not p.exists():
        return None
    data = json.loads(p.read_text())
    videos = [
        VideoRecord(
            **{k: v for k, v in vd.items() if k not in ("clips", "analytics")},
            clips=[ClipRecord(**c) for c in vd.get("clips", [])],
            analytics=vd.get("analytics", {}),
        )
        for vd in data.pop("videos", [])
    ]
    return SessionManifest(**data, videos=videos)


def save(session_dir: Path, manifest: SessionManifest) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    data = asdict(manifest)
    (session_dir / MANIFEST_NAME).write_text(json.dumps(data, indent=2))
