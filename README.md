# reo

A self-hosted web app to view **live** and **recorded** video from a Reolink
Home Hub, with Nest-style timeline scrubbing. Runs on your LAN; usable from PC
and phone.

## Architecture

```
Browser (React + TS, PC + phone)
   │ live (WebRTC)        │ recorded HD (fMP4 / MSE)      │ scrub preview (MP4)
   ▼                      ▼                               ▼
go2rtc ◄──spawns── FastAPI backend ──────────────────────────► Reolink Home Hub
 RTSP→WebRTC          • reolink_aio: Hub CGI + Baichuan API       (cameras + SD)
                      • Baichuan replay (TCP 9000) → demux → ffmpeg → fMP4
                      • sub-stream mirror: CGI download → local clip cache
```

- **Backend** (`backend/`): Python + FastAPI. Talks to the Hub via `reolink_aio`;
  spawns go2rtc for live; implements the native **Baichuan replay** protocol for
  recorded HD; runs a background sub-stream mirror for instant scrubbing.
- **go2rtc** (`bin/go2rtc.exe`): pulls camera RTSP and republishes as low-latency
  WebRTC for the **live** view; transcodes H.265→H.264 when a browser needs it.
- **Frontend** (`frontend/`): React + TypeScript. Live grid, plus a playback view
  with a zoomable timeline and Nest-style two-tier scrubbing.

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
- [x] **Phase 4** — Nest-style scrubbing: background sub mirror (recent + backfill
  + retention), real-time cross-segment scrubbing, timeline shows mirrored vs
  not-yet-mirrored footage, opens on the latest cached moment.
- [x] **Phase 5** — Native **Baichuan replay**: recorded **4K HEVC** streamed
  straight from the Hub (TCP 9000) to the browser via MSE — the fast HD path that
  replaces the slow CGI-download/transcode approach. Two-tier scrub (drag = cached
  sub preview, release = replay at that moment) and a **zoomable timeline** for
  precise seeking.
- [ ] **Next** — multi-camera layout, remote access, deploy to a permanent host
  (e.g. a Raspberry Pi), precise within-segment HD seeking.

## Recorded playback (Baichuan replay)

Recorded video plays over the Hub's native **Baichuan** protocol (TCP port 9000)
— the same path the Reolink desktop app uses, and the only fast route to the main
stream. The backend opens one replay on the shared Hub connection, tees the media
off the socket, demuxes the H.265 frames, pipes them through ffmpeg into a
fragmented MP4, and streams that to the browser's **MSE** SourceBuffer.

- **HEVC-capable browsers** (Chrome 107+ with a hardware HEVC decoder, Safari,
  modern phones) get **native 4K** — the replay is remuxed (`-c copy`, tagged
  `hvc1`), no transcode. Badge: "4K".
- **Other browsers** get a 1080p **H.264** transcode (`libx264`). Badge: "HD".

Clicking the timeline (re)opens the replay at the covering 5-min **segment start**
(~3–5 s to first frame, then smooth realtime 4K). True instant within-segment
seeking isn't possible on this Hub — Baichuan `cmd 123` can't cross the 5-min file
boundary, and the browser freezes on the mid-stream `hvc1` codec discontinuity —
so each seek reopens the stream; a watchdog retries a stuck open instead of
spinning forever.

> The replay media frame class varies by firmware (`416a` / `436a`); the backend's
> socket filter routes both. If playback goes black while the handshake otherwise
> succeeds, a new media class is the first thing to check.

## Two-tier scrubbing & timeline zoom

While you **drag** the timeline, the video shows the cached low-res **sub** clip
and seeks within it (instant once cached); when you **release**, it opens the
Baichuan replay (HD/4K) at that moment — Nest-style. The background mirror keeps
recent sub clips cached so dragging is instant over recent footage; uncached
regions cache on demand as you scrub into them.

The 24-hour timeline **zooms** for precise seeking (otherwise it's ~80 s/px):

- **scroll / pinch** to zoom, anchored on the cursor
- **double-click** to toggle a ~10-min window ↔ the full day
- when zoomed, an **overview strip** shows the whole day with a draggable viewport
  box to pan; tick spacing adapts (3 h → 5 m → 30 s) as you zoom in

## The sub-stream mirror

`app/mirror.py` pre-caches the lightweight sub stream to local disk
(`data/clips/<cam>/sub/<YYYY-MM-DD>/`) so scrubbing is instant. Three loops:
**recent** (keep the last ~30 min warm), **backfill** (walk back over recent days),
**retention** (drop cached days older than 14). HD/4K is never mirrored — it
streams live over Baichuan. Status at `GET /api/mirror`. The mirror is **on by
default**; set `REOLINK_MIRROR=0` to disable it (e.g. while debugging the Hub).

### Hub fragility & robustness

The Home Hub is easily overwhelmed: it has a small connection limit and its CGI
download path stalls under load. Mitigations:

- **Single connection** — all Hub traffic (live, replay, downloads) reuses one
  `reolink_aio` connection. Opening a connection per seek exhausts the Hub's
  session limit and wedges it until a reboot.
- **Mirror yields to playback** — while a replay stream is open the mirror starts
  no new downloads (for the whole stream, even if its media stalls), so playback
  gets the Hub's full bandwidth.
- **Gentle mirror** — background segments download one at a time with a 25 s gap.
- **Circuit breaker** — after repeated background failures the mirror pauses 5 min
  so the Hub can recover; interactive (user) requests ignore the breaker.
- **Stall detection** — downloads run via `curl` with a file-size throughput
  watchdog that kills a truly dead/dribbling transfer (below ~12 KB/s) while
  letting the Hub's normal ~38–44 KB/s sub downloads through.

If the Hub gets wedged anyway, a **reboot** restores it.

## Notes / known constraints

- The Hub has **no fast RTSP timestamp-seek** and **no ONVIF Profile G** — the
  fast recorded path is the proprietary Baichuan replay implemented here. (The CGI
  `Search`/`Download` API is still used to fetch sub clips for the mirror.)
- Recordings are dual-stream, 5-min segments: the 4K H.265 **main** stream plays
  over Baichuan; the low-res H.264 **sub** stream doubles as the scrub preview.
- The Hub serves CGI sub downloads at only ~38–44 KB/s (no HTTP Range), so the
  mirror caches recent footage first and fills history in over time.
- Battery cameras behind the Hub drop RTSP after ~5 min; wired cams stream 24/7.
- The camera image may appear rotated, depending on how it's mounted.
