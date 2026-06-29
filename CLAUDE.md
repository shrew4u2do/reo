# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`reo` is a self-hosted LAN web app to view **live** and **recorded** video from a
Reolink **Home Hub** (one fragile little NVR, currently 1 wired camera "Front
Door" on channel 0), with Nest-style timeline scrubbing. Usable from PC and phone
on the same Wi-Fi. Dev-only on this Windows PC for now.

The hard-won protocol/architecture history lives in the `reo-project` auto-memory
— read it before touching playback. Below is the operational big picture.

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

**There is no automated test suite.** `backend/test_*.py` and `backend/decrypt_capture.py`
are manual spike/diagnostic scripts (Baichuan protocol reversing, pcap parsing)
run directly with `.venv\Scripts\python test_xxx.py`. They are not pytest and are
not wired into CI. The backend has no linter configured.

**Process hygiene (important):** detached `Start-Process` restarts leave STALE
python processes serving OLD code — a frequent source of "my fix didn't work"
confusion. When restarting the backend, kill ALL python processes and verify a
single uvicorn process is running.

## Architecture

```
Browser (React/Vite)
  │  /api (HTTP)            │  WebRTC/HLS (live)        │  fMP4 over HTTP (recorded)
  ▼                        ▼                           ▼
FastAPI backend ──spawns──► go2rtc ──RTSP──► Hub        Baichuan TCP :9000 ──► Hub
  • reolink_aio Host (ONE shared connection to the Hub)
  • cache.py / mirror.py (CGI Download of sub clips to disk)
  • bcplay.py (native Baichuan replay → demux → ffmpeg → fMP4)
```

Three distinct video paths, each solving a different problem:

1. **Live** — `go2rtc.py` generates `bin/go2rtc.gen.yaml` (RTSP URLs *with Hub
   credentials* — gitignored) and runs `bin/go2rtc.exe` as a subprocess. go2rtc
   pulls camera RTSP and republishes WebRTC to the browser (`LiveTile.tsx` wraps
   the vendored `src/lib/video-rtc.js` web component).

2. **Recorded, sub-stream (scrub preview)** — `cache.py` (`ClipCache`) downloads
   5-min low-res segments via the Hub's slow CGI `Download` (~38 KB/s, no Range)
   to `data/clips/<cam>/<stream>/<YYYY-MM-DD>/<sha1>.mp4`, remuxes with ffmpeg,
   serves seekably. `mirror.py` pre-caches recent days in the background.
   Endpoints: `/api/recordings`, `/api/clip`, `/api/prefetch`.

3. **Recorded, main-stream HD/4K (the hard part)** — `bcplay.py` implements the
   proprietary **Baichuan replay protocol** (the only fast path to main-stream
   recordings; CGI Download is too slow for 4K). It drives `reolink_aio`'s
   `host.baichuan.send(...)`, tees the raw socket to capture class-416a media,
   demuxes BcMedia/H.265 frames (`bc_demux.py`), pipes through ffmpeg to hvc1
   fragmented MP4, and streams it to the browser's MSE (`src/lib/mse.ts`).
   `bc_replay.py` holds the cmd14/15/123/5 handshake helpers.
   Endpoints: `/api/bcplay`, `/api/bcplay/seek`.

`hub.py` (`HubManager`, singleton `hub`) owns the single `reolink_aio.Host`.
`config.py` reads Hub host/creds from `backend/.env` (gitignored). `main.py`
wires the lifespan: connect hub → start go2rtc → start mirror → pre-warm a replay
connection. The frontend's top-level state (`App.tsx` live grid ↔
`PlaybackView.tsx` timeline) and `api.ts` are the client entry points.

## Non-negotiable constraints (do not re-litigate — see memory for the full why)

- **ONE Hub connection.** The Home Hub has a tiny max-session limit (rspCode -5)
  and frees connections slowly. Reuse `hub.host` for everything; NEVER open a
  connection-per-seek — it exhausts sessions and wedges the Hub until reboot.
  `logout()` on one connection disrupts others.
- **The permanent media filter** in `bcplay.py` (`_install_filter`) strips
  Baichuan replay media (class 416a) from reolink_aio's command parser (which
  would otherwise desync) and routes it to the active stream. Never remove it.
- **Playback file list must come from `request_vod_files`** (full-day CGI search),
  NOT the Baichuan cmd14/15 list (truncated to ~40 files → seeks past ~03:16
  clamp to the same segment).
- **In-place cmd123 re-seek does not work** for full scrubbing: cmd123 can't
  cross the 5-min file boundary, and the browser freezes on the hvc1 codec
  discontinuity mid-file. So each seek reopens the replay at the covering 5-min
  segment start (~3-5s to first frame, then smooth realtime 4K). This is the
  accepted current behavior, not a bug to "fix" naively.

## Gotchas

- **bcplay streaming must hit backend `:8000` directly**, not through the Vite
  `/api` proxy — the proxy buffers streaming responses and breaks the fetch.
- ffmpeg for replay needs `-probesize 50000 -analyzeduration 0
  -fflags +genpts+nobuffer -movflags frag_every_frame+empty_moov+default_base_moof
  -flush_packets 1` or it buffers and emits nothing on the slow pipe; and
  `-tag:v hvc1` so the MSE codec string (`hvc1.1.6.L186.B0`, Hub HEVC is **L186**)
  matches. MSE needs `hvc1`, not `hev1`.
- `bin/go2rtc.gen.yaml` and `backend/.env` contain the Hub password — both
  gitignored; never commit or echo them.
- winget-installed tools (node, ffmpeg) aren't on the inherited PATH of processes
  launched from stale shells; `start.ps1` rebuilds PATH and `go2rtc.py` resolves
  ffmpeg's absolute path.
- The camera is mounted sideways (image rotated 90°) — a known polish-phase item.
```
