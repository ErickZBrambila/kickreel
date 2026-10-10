from dataclasses import dataclass

import requests

from . import config


@dataclass
class PublishedVideo:
    file_name: str
    r2_url: str
    thumbnail_url: str
    duration: float
    resolution: str
    markers: list[dict]
    clips: list[dict]
    analytics: dict | None = None


def create_session_document(
    match_id: str,
    session_name: str,
    session_type: str,
    session_date: str,
    team_id: str,
    videos: list[PublishedVideo],
) -> str:
    doc_id = f"practiceSession-{match_id}"

    video_docs = []
    for i, v in enumerate(videos):
        vdoc: dict = {
            "_key": f"video-{i}",
            "fileName": v.file_name,
            "r2Url": v.r2_url,
            "thumbnailUrl": v.thumbnail_url,
            "durationSeconds": round(v.duration, 2),
            "resolution": v.resolution,
            "markers": [
                {"_key": f"m-{i}-{j}", "timeSeconds": m["timeSeconds"], "label": m["label"]}
                for j, m in enumerate(v.markers)
            ],
            "clips": [
                {
                    "_key": f"c-{i}-{j}",
                    "r2Url": c["r2Url"],
                    "startSeconds": c["startSeconds"],
                    "endSeconds": c["endSeconds"],
                    "label": c["label"],
                    **({"clipType": c["clipType"]} if c.get("clipType") else {}),
                }
                for j, c in enumerate(v.clips)
            ],
        }
        if v.analytics:
            a = v.analytics
            analytics_doc: dict = {
                "fieldZones": a.get("fieldZones", []),
                "goalPressureLeft": a.get("goalPressureLeft", 0),
                "goalPressureRight": a.get("goalPressureRight", 0),
                "totalFrames": a.get("totalFrames", 0),
                "dominantZone": a.get("dominantZone", 0),
                "activityBuckets": [
                    {"_key": f"ab-{i}-{k}", **b}
                    for k, b in enumerate(a.get("activityBuckets", []))
                ],
            }
            # Per-half fields (added in Phase 2 of analyze step)
            for key in (
                "fieldZonesFirstHalf", "fieldZonesSecondHalf",
                "goalPressureLeftH1", "goalPressureRightH1",
                "goalPressureLeftH2", "goalPressureRightH2",
            ):
                if key in a:
                    analytics_doc[key] = a[key]
            vdoc["analytics"] = analytics_doc
        video_docs.append(vdoc)

    doc: dict = {
        "_id": doc_id,
        "_type": "practiceSession",
        "sessionName": session_name,
        "sessionType": session_type,
        "sessionDate": session_date,
        "videos": video_docs,
    }
    if team_id:
        doc["team"] = {"_type": "reference", "_ref": team_id}

    url = (
        f"https://{config.SANITY_PROJECT_ID}.api.sanity.io"
        f"/v2024-01-01/data/mutate/{config.SANITY_DATASET}"
    )
    resp = requests.post(
        url,
        json={"mutations": [{"createOrReplace": doc}]},
        headers={
            "Authorization": f"Bearer {config.SANITY_WRITE_TOKEN}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return doc_id
