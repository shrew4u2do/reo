# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`reo` is a self-hosted LAN web app to view **live** and **recorded** video from a
Reolink **Home Hub** (one fragile little NVR, currently 1 wired camera "Front
Door" on channel 0), with Nest-style timeline scrubbing. Usable from PC and phone
on the same Wi-Fi. Dev-only on this Windows PC for now.

Live comes from the Hub (go2rtc → WebRTC). **Recorded video is read from a NAS
share** (`Z:\reo`, SMB-mounted) that the Hub FTP-uploads to — the Hub is no longer
in the playback path. (The previous design replayed main-stream over the
reverse-engineered Baichuan protocol; that whole stack was removed — see the
`reo-project` auto-memory for that history.) Below is the operational big picture.

## Commands

Run everything (backend + go2rtc + Vite) from the repo root:
```
powershell -ExecutionPolicy Bypass -File start.ps1
```
Then open http://localhost:5173 (PC) or http://<PC-LAN-IP>:5173 (phone).
Ports: backend FastAPI **:8000**, go2rtc **:1984**, Vite **:5173**.

Backend alone (from `backend/`):
```
.venv\Scripts\python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
.venv\Scripts\python test_connection.py      # verify Hub connectivity standalone
```
Recreate the venv: `python -m venv .venv && .venv\Scripts\python -m pip install -r requirements.txt`

Frontend (from `frontend/`):
```
npm install
npm run dev        # Vite dev server (host:true → reachable from phone)
npm run build      # tsc -b && vite build
npm run lint       # oxlint
```

**There is no automated test suite.** `backend/test_connection.py` verifies Hub
connectivity. The other `backend/test_*.py`, `backend/decrypt_capture.py`, and the
top-level `backend/bc_*.py` are now-obsolete Baichuan protocol-reversing spikes,
kept only for history — they import deleted modules and don't run. Nothing here is
pytest or wired into CI. The backend has no linter configured.

**Process hygiene (important):** detached `Start-Process` restarts leave STALE
python processes serving OLD code — a frequent source of "my fix didn't work"
confusion. When restarting the backend, kill ALL python processes and verify a
single uvicorn process is running.

## Architecture

```
Browser (React/Vite)
  │  /api (HTTP)            │  WebRTC/HLS (live)        │  MP4 over HTTP (recorded)
  ▼                        ▼                           ▼
FastAPI backend ──spawns──► go2rtc ──RTSP──► Hub        NAS (SMB Z:\reo) ── read MP4/JPEG
  • reolink_aio Host (ONE shared connection to the Hub — live + camera list only)
  • nas.py (index NAS segments by filename; lazy ffmpeg remux → seekable MP4)
  • mirror.py (pre-remux recent segments; purge the local remux cache)
```

Two video paths:

1. **Live** — `go2rtc.py` generates `bin/go2rtc.gen.yaml` (RTSP URLs *with Hub
   credentials* — gitignored) and runs `bin/go2rtc.exe` as a subprocess. go2rtc
   pulls camera RTSP and republishes WebRTC to the browser (`LiveTile.tsx` wraps
   the vendored `src/lib/video-rtc.js` web component). Unchanged by the NAS work.

2. **Recorded** — the Hub FTP-uploads recordings to the NAS (`REOLINK_NAS_ROOT`,
   default `Z:\reo`): one folder per day of ~10-min **4K HEVC + AAC fragmented
   MP4** segments plus **per-minute JPEG snapshots**, named
   `<Cam>_<NN>_<Hub>_<YYYYMMDDHHMMSS>.{mp4,jpg}`. `nas.py` (`NasLibrary`) indexes
   them from the filenames (durations come from neighbouring start times — no
   per-file probe). The fragmented MP4s carry no seek index (`sidx`/`mfra`), so
   each is lazily remuxed — `ffmpeg -c copy` (native 4K HEVC), or 1080p H.264 for
   browsers without an HEVC decoder — into a seekable MP4 cached under
   `data/clips/<cam>/<mode>/<YYYY-MM-DD>/`, served with HTTP Range. The JPEGs are
   the scrub-bar preview. `mirror.py` pre-remuxes recent segments.
   Endpoints: `/api/recordings`, `/api/clip`, `/api/thumbnail`, `/api/prefetch`.

`hub.py` (`HubManager`, singleton `hub`) owns the single `reolink_aio.Host` — now
used only for live RTSP and the camera list. `config.py` reads Hub creds + NAS root
from `backend/.env` (gitignored). `main.py` wires the lifespan: connect hub → start
go2rtc → start mirror. The frontend's top-level state (`App.tsx` live grid ↔
`PlaybackView.tsx` timeline) and `api.ts` are the client entry points.

## Non-negotiable constraints (do not re-litigate — see memory for the full why)

- **ONE Hub connection.** The Home Hub has a tiny max-session limit (rspCode -5)
  and frees connections slowly. Reuse `hub.host`; never open a connection-per-call.
  This now only affects **live** (RTSP source URLs + camera list) — playback no
  longer touches the Hub at all.
- **The NAS is read-only.** The backend only reads `Z:\reo`; the Hub/NAS owns
  recording + retention there. Only `data/clips/` (the local remux cache) is ours
  to write/purge.
- **Fragmented MP4 needs a remux to seek.** The NAS MP4s have no `sidx`/`mfra`, so
  a plain `<video>` can't seek the raw file — keep the `ffmpeg` remux-to-cache step
  (`nas.py`); don't "simplify" by serving the NAS file directly.
- **Don't `ffprobe` per segment** when building `/api/recordings`. Durations come
  from neighbouring segment start times (the last/growing one from file mtime); a
  per-file probe on a full day is needlessly slow.

## Gotchas

- **Browser HEVC support varies.** Native 4K (`-c copy`) needs an HEVC decoder
  (Safari; Edge with the HEVC Video Extensions; Chrome with hardware HEVC). The
  frontend probes `video.canPlayType('…hvc1.1.6.L186.B0…')` (Hub HEVC is **L186**)
  and falls back to `hevc=0` → backend 1080p H.264 transcode (slower per segment).
  Keep `-tag:v hvc1` on the copy path so the codec string matches; use `hvc1`, not
  `hev1`.
- `bin/go2rtc.gen.yaml` and `backend/.env` contain the Hub password — both
  gitignored; never commit or echo them.
- winget-installed tools (node, ffmpeg) aren't on the inherited PATH of processes
  launched from stale shells; `start.ps1` rebuilds PATH and `go2rtc.py` resolves
  ffmpeg's absolute path.
- The camera is mounted sideways (image rotated 90°) — a known polish-phase item.
```
