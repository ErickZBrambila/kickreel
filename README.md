# KickReel

Automated soccer highlight pipeline for the **Falcon XBotGo** camera. Reads proprietary SD card files, extracts highlight clips, uploads to Cloudflare R2, and publishes to the North Legends FC website ([legends-pitch](../legends-pitch)).

Parents get a dedicated `/videos` page on the team site where they can watch and download every highlight clip from practices and games — no subscription required.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│                        FALCON XBotGo Camera                         │
│                                                                     │
│  SD Card                                                            │
│  ├── 20260929/                                                      │
│  │   ├── VID_*.mp4          ← 4K / 1080p HEVC video                │
│  │   └── .data/                                                     │
│  │       ├── *.mark         ← coach marker events (JSON)            │
│  │       ├── *.vdata        ← video metadata (JSON)                 │
│  │       ├── *.cover        ← pre-built JPEG thumbnails             │
│  │       └── *.track        ← per-frame camera telemetry (text)     │
│  └── .data/match/                                                   │
│      └── *.match            ← match/session metadata (JSON)         │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                    ① offload
                            │
                            ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     LOCAL LIBRARY  ~/Movies/KickReel/               │
│                                                                     │
│  20260929_<match_id>/                                               │
│  ├── footage/                                                       │
│  │   └── VID_*.mp4          ← full-res copies (USB shareable)      │
│  ├── thumbs/                                                        │
│  │   └── VID_*.cover.jpg    ← session thumbnails                   │
│  ├── clips/                 ← created by ② process                 │
│  │   └── VID_*_clip_0108s.mp4                                      │
│  └── kickreel.json          ← manifest: tracks pipeline state      │
│                               { offloaded, processed, published }   │
└──────────────┬───────────────────────────────────────────────────┬──┘
               │                                                   │
       ② process                                              USB hand-off
       (ffmpeg -c copy)                                    to parents
               │
               ▼
┌─────────────────────────────────────────────────────────────────────┐
│                           HIGHLIGHT CLIPS                           │
│                                                                     │
│  Each coach marker click  →  30-second lossless clip               │
│  Duplicates within 1s deduped automatically                         │
└───────────────────────────┬─────────────────────────────────────────┘
                            │
                    ③ publish
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
┌──────────────────────┐    ┌────────────────────────────┐
│   Cloudflare R2      │    │       Sanity CMS           │
│                      │    │                            │
│  kickreel/           │    │  practiceSession document  │
│  └── sessions/       │    │  ├── sessionName           │
│      └── {date}/     │    │  ├── sessionType           │
│          ├── clips/  │    │  ├── sessionDate           │
│          │  └── *.mp4│    │  └── videos[]              │
│          └── thumbs/ │    │      ├── thumbnailUrl      │
│             └── *.jpg│    │      ├── markers[]         │
│                      │    │      └── clips[]           │
│  Zero egress fees    │    │          ├── r2Url         │
│  ~$0/month free tier │    │          ├── startSeconds  │
└──────────────────────┘    │          └── label         │
                            └────────────┬───────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────┐
│              legends-pitch  (Next.js on Vercel)                     │
│                                                                     │
│  /videos                                                            │
│  └── Session grid: thumbnail · name · date · clip count            │
│                                                                     │
│  /videos/[sessionId]                                                │
│  ├── Video player (HTML5, streams from R2)                          │
│  ├── Marker chips → click to seek                                   │
│  ├── Highlight clips grid                                           │
│  │   └── Play · Download (30s, full quality)                        │
│  └── Open to all parents — no login required                        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Three-step pipeline

### ① Offload — free the SD card immediately
```bash
python pipeline.py offload \
  --source /Volumes/Untitled/falcon \
  --library ~/Movies/KickReel
```
Copies all footage and thumbnails to your Mac. Takes ~5 minutes for a full session. SD card is free to reuse right after.

### ② Process — extract highlight clips (no internet needed)
```bash
python pipeline.py process --library ~/Movies/KickReel
```
Reads every `kickreel.json` manifest, finds unprocessed sessions, and cuts 30-second lossless clips around each coach marker using `ffmpeg -c copy`. Fast — a full session processes in under a minute.

Use `--pad 20` to get 40-second clips instead of 30.

### ③ Publish — push to the web (needs internet)
```bash
python pipeline.py publish --library ~/Movies/KickReel --team u11
```
Uploads clips + thumbnails to Cloudflare R2, then creates/updates `practiceSession` documents in Sanity. The `/videos` page on the team site updates immediately.

Re-running any step is safe — manifests track `offloaded`, `processed`, and `published` flags and skip already-completed sessions.

---

## Setup

### 1. Install dependencies
```bash
pip install -r requirements.txt
```
Also requires `ffmpeg` installed: `brew install ffmpeg`

### 2. Configure credentials
```bash
cp .env.example .env
```
Fill in `.env`:
```
R2_ACCOUNT_ID=           # Cloudflare account ID
R2_ACCESS_KEY_ID=        # R2 API token access key
R2_SECRET_ACCESS_KEY=    # R2 API token secret
R2_BUCKET=kickreel-videos
R2_PUBLIC_URL=           # https://pub-XXXX.r2.dev (or custom domain)

SANITY_PROJECT_ID=       # from legends-pitch .env
SANITY_DATASET=production
SANITY_WRITE_TOKEN=      # Sanity API token with write access

SANITY_TEAM_U11_ID=      # Sanity document _id for U11 team
SANITY_TEAM_U12_ID=      # Sanity document _id for U12 team
```

### 3. Add R2 subscription in Cloudflare
R2 → **Add R2 subscription**. Free tier covers 10 GB/month. A full season of 30-second clips stays under $0.50/month.

---

## SD card file formats (Falcon XBotGo proprietary)

| Extension | Format | Contents |
|---|---|---|
| `.mark` | JSON | `[{type, time}]` — coach button taps during recording |
| `.vdata` | JSON | Video metadata: duration, fps, resolution, `match_id`, `has_mark` |
| `.match` | JSON | Session info: name, type (PRACTICE/GAME), teams, start/end, video list |
| `.cover` | JPEG | Pre-built thumbnail (~10 KB), generated by camera |
| `.track` | Text | Per-frame telemetry: UTC timestamp, Yaw/Pitch, ball detection, GoalGate detection |

---

## Local library structure

```
~/Movies/KickReel/
└── 2026-09-29_1877710259470134/     ← date_matchId
    ├── kickreel.json                 ← pipeline manifest
    ├── footage/
    │   └── VID_20260929_182102_01_01.mp4   ← full-res (USB shareable)
    ├── thumbs/
    │   └── VID_20260929_182102_01_01.jpg
    └── clips/
        ├── VID_20260929_182102_01_01_clip_0108s.mp4
        └── VID_20260929_182102_01_01_clip_0182s.mp4
```

---

## Roadmap

### Phase 2 — Auto-events from `.track` files
The camera writes per-frame telemetry logs with ball detection status, camera Yaw/Pitch, and GoalGate proximity. No computer vision needed — just log parsing.

Planned auto-events:
- **Ball found runs** — sustained `Ball1 Found` frames → possession sequences
- **GoalGate events** — camera panning toward goal + ball detected → likely shot or goal
- **Action bursts** — rapid Yaw-change sequences → camera tracking fast play

### Phase 3 — Team site enhancements
- Session summary stats (total markers, session duration)
- Email/SMS notification to parents when new clips are published
- Clip sharing — individual shareable link per clip
