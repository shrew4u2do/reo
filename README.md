# reo

A self-hosted web app to view **live** and **recorded** video from a Reolink
Home Hub, with Nest-style timeline scrubbing. Runs on your LAN; usable from PC
and phone.

## Architecture

```
Browser (PC + phone)  ── React + TypeScript (Vite)
        │  /api  (HTTP)              │  WebRTC/HLS
        ▼                           ▼
FastAPI backend  ──spawns──►  go2rtc  ──RTSP──►  Reolink Home Hub
  • reolink_aio (Hub CGI/Baichuan API)            (cameras + SD card)
  • recording search / playback (Phase 3+)
```

- **Backend** (`backend/`): Python + FastAPI. Talks to the Hub via
  `reolink_aio`; generates the go2rtc config and runs go2rtc as a subprocess.
- **go2rtc** (`bin/go2rtc.exe`): pulls camera RTSP and republishes as
  low-latency WebRTC to browsers; transcodes H.265→H.264 when needed.
- **Frontend** (`frontend/`): React + TypeScript live grid; timeline scrubber
  (Phase 4).

## Prerequisites

Already installed on this machine: Python 3.14, Node 24, ffmpeg, go2rtc.

## Setup

1. `cd backend && copy .env.example .env`, then edit `.env` with your Hub IP,
   username, and password. (`.env` is gitignored.)
2. Backend deps: `backend\.venv` is already created. To recreate:
   `python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt`
3. Frontend deps: `cd frontend && npm install`

## Run

From the project root:

```
powershell -ExecutionPolicy Bypass -File start.ps1
```

Then open **http://localhost:5173** on this PC, or
**http://<this-pc-LAN-IP>:5173** on your phone (same Wi-Fi).

To verify the Hub connection on its own: `cd backend && .venv\Scripts\python test_connection.py`

## Status / roadmap

- [x] **Phase 1** — Foundation: toolchain, backend scaffold, Hub connectivity.
- [x] **Phase 2** — Live view: go2rtc WebRTC live grid (responsive).
- [x] **Phase 3** — Timeline + recorded playback: sub-stream segments cached to
  disk (seekable, Range), real-time in-segment scrubbing, date nav, prefetch.
- [x] **Phase 4** — Nest-style scrubbing: background sub mirror (14-day,
  recent+backfill+retention), real-time cross-segment scrubbing, timeline shows
  mirrored vs not-yet-mirrored footage, opens on latest cached moment.
- [x] **Phase 5a** — HD-on-settle: scrub on sub, and when you settle the picture
  upgrades to 1080p (4K H.265 → H.264 transcode), with sub playing until HD is
  ready. Interactive HD fetches preempt the background mirror.
- [ ] **Phase 5b** — Timeline zoom (precise seeking on a 24h bar), multi-cam,
  remote access, deploy to a permanent host.

## HD-on-settle

The main stream is 4K **H.265**. The frontend feature-detects HEVC support
(`supportsHevc()` via `canPlayType`) and picks the best path:

- **HEVC-capable browsers** (Chrome 107+ with a hardware HEVC decoder, Safari,
  modern phones) get **native 4K** — the main segment is only remuxed (`-c copy`,
  tagged `hvc1`), no transcode. Badge: "4K".
- **Other browsers** get a 1080p **H.264** transcode (`libx264 veryfast`,
  ~15x realtime). Badge: "HD".

Either way the clip is downloaded from the Hub on demand and cached seekably
under `data/clips/<cam>/{main,hd}/`. **HD is opt-in via a "Load HD/4K" button** —
not automatic — because an un-cached HD segment takes minutes to fetch (the Hub
serves recordings at only ~75–150 KB/s and a 4K segment is 30–40 MB; there's no
mid-size stream and `NvrDownload` for partial fetches is rejected by the
firmware). Scrubbing always stays on the instant sub stream; clicking Load HD
shows a download **progress %**, keeps sub playing until ready, then swaps.
Cached HD is instant on revisit.

### Hub fragility & robustness

The Home Hub falls over (downloads stall to ~0 B/s) under sustained concurrent
recording requests. Mitigations:

- **Gentle mirror**: background segments download one at a time with a 4 s gap.
- **Circuit breaker**: after repeated background failures the mirror pauses 5 min
  so the Hub can recover. Interactive (user) requests ignore the breaker.
- **Interactive priority**: a user fetch uses a separate slot and pauses the
  mirror while active, so it gets the Hub's full bandwidth.
- **Stall detection**: downloads run via `curl` with a file-size throughput
  watchdog (kills a transfer below ~43 KB/s), and the frontend has its own
  throughput watchdog so it shows "Hub busy / too slow — try again" within ~35 s
  instead of spinning. 20 s retry cooldown after a failure.

If the Hub gets wedged anyway, a **reboot** restores it.

Download scheduling (`app/cache.py`): interactive playback requests use a
separate slot from the background mirror and pause the mirror while active, so a
user fetch is never stuck behind the mirror. Total Hub concurrency stays at 2 —
the Hub becomes unstable under heavier concurrent load.

## Mirror

`app/mirror.py` runs three background loops: **recent** (cache the last ~30 min
every 2 min), **backfill** (walk back over the retention window caching all
sub segments), **retention** (drop cached days older than 14). HD/main is fetched
on demand. Cache lives in `data/clips/<cam>/<stream>/<YYYY-MM-DD>/`. Status at
`GET /api/mirror`. Initial backfill of history is slow (~38 KB/s sub) — recent
footage caches first, history fills in over a day or two.

## The key constraint: Hub recording speed

The Home Hub serves recordings at **~38 KB/s** with **no HTTP Range** support.
That's ~5.7x realtime for the **sub** stream (so caching + sequential play work
well) but only ~0.5x realtime for the **main/HD** stream (so on-demand HD is
impractical). The app caches each sub segment locally on first view, then serves
it seekably. Smooth full-day scrubbing and HD both point toward a **background
local mirror** (continuously copy recordings off the Hub) — the Phase 4/5 work.

## Notes / known constraints

- The Hub has **no fast RTSP timestamp-seek** and **no ONVIF Profile G**.
  Recordings are accessed via the CGI API: `Search` → `NvrDownload` (assembles
  an arbitrary range, 1s precision) → `Download` MP4 (HTTP Range supported).
- Recordings are dual-stream: the low-res **sub** stream doubles as the scrub
  **preview track** (Phase 4).
- Battery cameras behind the Hub drop RTSP after ~5 min; wired cams stream 24/7.
