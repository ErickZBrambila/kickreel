#!/usr/bin/env python3
"""
KickReel MCP Server

Exposes the KickReel pipeline and library to Claude Code so you can inspect,
process, and publish soccer sessions without leaving the editor.

Tools:
  list_sessions       — list all sessions in the local library with pipeline status
  get_session         — full manifest for one session
  get_pipeline_status — counts: offloaded / processed / published / needs attention
  run_process         — extract highlight clips from offloaded footage
  run_publish         — upload clips to R2 and push metadata to Sanity
  query_sanity        — list practiceSession docs live on legends-pitch

Run:
  fastmcp run mcp_server.py
"""

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

from dotenv import load_dotenv
from fastmcp import FastMCP

# Load credentials from kickreel/.env so Sanity tools work
load_dotenv(Path(__file__).parent / ".env")

sys.path.insert(0, str(Path(__file__).parent))
from kickreel import manifest as manifest_mod

mcp = FastMCP("kickreel")

DEFAULT_LIBRARY = str(Path.home() / "Movies" / "KickReel")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _library_path(library: str | None) -> Path:
    return Path(library or DEFAULT_LIBRARY).expanduser()


def _load_all_sessions(library: Path) -> list[manifest_mod.SessionManifest]:
    sessions = []
    for d in sorted(library.iterdir()):
        if d.is_dir():
            m = manifest_mod.load(d)
            if m:
                sessions.append(m)
    return sessions


def _session_summary(m: manifest_mod.SessionManifest) -> dict:
    clip_count = sum(len(v.clips) for v in m.videos)
    marker_count = sum(len(v.markers) for v in m.videos)
    enriched_count = sum(1 for v in m.videos for c in v.clips if c.player_name)
    has_track_files = any(v.track_path for v in m.videos)
    return {
        "match_id": m.match_id,
        "session_name": m.session_name,
        "session_type": m.session_type,
        "session_date": m.session_date,
        "home_team": m.home_team,
        "away_team": m.away_team,
        "offloaded": m.offloaded,
        "processed": m.processed,
        "published": m.published,
        "sanity_doc_id": m.sanity_doc_id,
        "video_count": len(m.videos),
        "marker_count": marker_count,
        "clip_count": clip_count,
        "enriched_clips": enriched_count,
        "can_enrich": has_track_files and clip_count > 0,
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def list_sessions(library: str = DEFAULT_LIBRARY) -> list[dict]:
    """List all sessions in the local KickReel library with their pipeline status.

    Args:
        library: Path to the local library (default: ~/Movies/KickReel)
    """
    p = _library_path(library)
    if not p.exists():
        return [{"error": f"Library not found: {p}"}]
    sessions = _load_all_sessions(p)
    return [_session_summary(m) for m in sessions]


@mcp.tool()
def get_session(match_id: str, library: str = DEFAULT_LIBRARY) -> dict:
    """Get the full manifest for a specific session.

    Args:
        match_id: The match ID (directory name suffix after the date_)
        library:  Path to the local library (default: ~/Movies/KickReel)
    """
    p = _library_path(library)
    for d in p.iterdir():
        if d.is_dir() and match_id in d.name:
            m = manifest_mod.load(d)
            if m:
                return asdict(m)
    return {"error": f"No session found with match_id containing '{match_id}'"}


@mcp.tool()
def get_pipeline_status(library: str = DEFAULT_LIBRARY) -> dict:
    """Return a summary of how many sessions are at each pipeline stage.

    Args:
        library: Path to the local library (default: ~/Movies/KickReel)
    """
    p = _library_path(library)
    if not p.exists():
        return {"error": f"Library not found: {p}"}

    sessions = _load_all_sessions(p)
    offloaded = [s for s in sessions if s.offloaded]
    processed = [s for s in sessions if s.processed]
    published = [s for s in sessions if s.published]
    needs_processing = [s for s in sessions if s.offloaded and not s.processed]
    needs_publishing = [s for s in sessions if s.processed and not s.published]

    return {
        "total": len(sessions),
        "offloaded": len(offloaded),
        "processed": len(processed),
        "published": len(published),
        "needs_processing": [_session_summary(s) for s in needs_processing],
        "needs_publishing": [_session_summary(s) for s in needs_publishing],
    }


@mcp.tool()
def find_tracker_files() -> list[dict]:
    """Find KickReel Stats tracker JSON files available in ~/Downloads.

    Returns each file's path, date, opponent, and event count so you can
    pick the right one before calling run_enrich.
    """
    downloads = Path.home() / "Downloads"
    results = []
    for f in sorted(downloads.glob("kickreel-*.json"), reverse=True):
        try:
            data = json.loads(f.read_text())
            results.append({
                "path": str(f),
                "file": f.name,
                "date": data.get("date", ""),
                "opponent": data.get("opponent", ""),
                "event_count": len(data.get("events", [])),
            })
        except Exception as exc:
            results.append({"path": str(f), "file": f.name, "error": str(exc)})
    return results


@mcp.tool()
def run_enrich(
    tracker: str = "",
    library: str = DEFAULT_LIBRARY,
    match_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Tag clips with player names by matching tracker events to camera markers.

    Requires a KickReel Stats tracker JSON (from ~/Downloads/kickreel-*.json).
    If tracker is omitted the most recent file in ~/Downloads is used automatically.
    Only sessions with .track files on their videos can be enriched.

    Args:
        tracker:  Path to a KickReel Stats tracker JSON. Empty = auto-detect latest.
        library:  Path to the local library (default: ~/Movies/KickReel)
        match_id: Only enrich this match ID (empty = auto-match by date in tracker)
        dry_run:  Show matches without writing to manifests
    """
    # Auto-detect tracker if not provided
    if not tracker:
        downloads = Path.home() / "Downloads"
        candidates = sorted(downloads.glob("kickreel-*.json"), reverse=True)
        if not candidates:
            return {"error": "No kickreel-*.json found in ~/Downloads. Pass tracker= explicitly."}
        tracker = str(candidates[0])

    tracker_path = Path(tracker).expanduser()
    if not tracker_path.exists():
        return {"error": f"Tracker file not found: {tracker_path}"}

    cmd = [
        sys.executable, str(Path(__file__).parent / "pipeline.py"),
        "enrich",
        "--library", str(_library_path(library)),
        "--tracker", str(tracker_path),
    ]
    if match_id:
        cmd += ["--match-id", match_id]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent))
    return {
        "tracker_used": str(tracker_path),
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "success": result.returncode == 0,
    }


@mcp.tool()
def run_process(
    library: str = DEFAULT_LIBRARY,
    match_id: str = "",
    pad: int = 15,
    dry_run: bool = False,
    remove_watermark: bool = False,
) -> dict:
    """Extract highlight clips from offloaded footage using ffmpeg.

    Args:
        library:          Path to the local library (default: ~/Movies/KickReel)
        match_id:         Only process this match ID (empty = all unprocessed)
        pad:              Seconds of padding before/after each marker (default: 15)
        dry_run:          Show what would be done without running ffmpeg
        remove_watermark: Blur the XBotGo watermark (slower — re-encodes video)
    """
    cmd = [
        sys.executable, str(Path(__file__).parent / "pipeline.py"),
        "process",
        "--library", str(_library_path(library)),
        "--pad", str(pad),
    ]
    if match_id:
        cmd += ["--match-id", match_id]
    if dry_run:
        cmd.append("--dry-run")
    if remove_watermark:
        cmd.append("--remove-watermark")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent))
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "success": result.returncode == 0,
    }


@mcp.tool()
def run_publish(
    team: str = "u11",
    library: str = DEFAULT_LIBRARY,
    match_id: str = "",
    dry_run: bool = False,
) -> dict:
    """Upload clips to Cloudflare R2 and push metadata to Sanity (legends-pitch).

    Args:
        team:     Team to publish under — "u11" or "u12" (default: u11)
        library:  Path to the local library (default: ~/Movies/KickReel)
        match_id: Only publish this match ID (empty = all unpublished)
        dry_run:  Show what would be uploaded without touching R2 or Sanity
    """
    if team not in ("u11", "u12"):
        return {"error": f"Invalid team '{team}'. Must be 'u11' or 'u12'."}

    cmd = [
        sys.executable, str(Path(__file__).parent / "pipeline.py"),
        "publish",
        "--library", str(_library_path(library)),
        "--team", team,
    ]
    if match_id:
        cmd += ["--match-id", match_id]
    if dry_run:
        cmd.append("--dry-run")

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(Path(__file__).parent))
    return {
        "exit_code": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
        "success": result.returncode == 0,
    }


@mcp.tool()
def query_sanity(team: str = "") -> list[dict]:
    """List practiceSession documents published to legends-pitch via Sanity.

    Args:
        team: Filter by team slug ("u11" or "u12"). Empty = all teams.
    """
    project_id = os.environ.get("SANITY_PROJECT_ID", "")
    dataset = os.environ.get("SANITY_DATASET", "production")
    token = os.environ.get("SANITY_WRITE_TOKEN", "")

    if not project_id or not token:
        return [{"error": "SANITY_PROJECT_ID or SANITY_WRITE_TOKEN not set in kickreel/.env"}]

    import urllib.parse
    import requests

    groq = '*[_type == "practiceSession"]{_id, sessionName, sessionType, sessionDate, "team": team->slug.current, "clipCount": length(videos[].clips[]), "videoCount": length(videos)}'
    url = f"https://{project_id}.api.sanity.io/v2021-10-21/data/query/{dataset}?query={urllib.parse.quote(groq)}"

    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        resp.raise_for_status()
        results = resp.json().get("result", [])
        if team:
            results = [r for r in results if r.get("team") == team]
        return sorted(results, key=lambda r: r.get("sessionDate") or "", reverse=True)
    except Exception as exc:
        return [{"error": str(exc)}]


if __name__ == "__main__":
    mcp.run()
