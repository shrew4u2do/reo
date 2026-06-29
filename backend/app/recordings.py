"""Recording search + clip proxy endpoints (Phase 3).

  GET /api/recordings?camera=<slug>&date=YYYY-MM-DD&stream=sub
      -> { date, stream, availableDates, segments:[{id,start,end,durationSec,clock}] }

  GET /api/clip?camera=<slug>&id=<token>&stream=sub
      -> streams the decrypted MP4 for one segment, forwarding HTTP Range so
         the browser can seek within it.

The segment `id` is base64url(file_name); the Hub's recording path carries no
credentials, but we still validate the shape before handing it to Download.
"""
from __future__ import annotations

import base64
import binascii
from datetime import datetime, time

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse

from .cache import clip_cache
from .hub import hub
from . import bcplay

router = APIRouter()


def _encode_id(file_name: str) -> str:
    return base64.urlsafe_b64encode(file_name.encode()).decode().rstrip("=")


def _decode_id(token: str) -> str:
    pad = "=" * (-len(token) % 4)
    try:
        name = base64.urlsafe_b64decode(token + pad).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise HTTPException(400, "bad segment id") from exc
    # Guard: only allow real recording paths through to Download.
    if "Mp4Record" not in name or not name.endswith((".vref", ".mp4")):
        raise HTTPException(400, "invalid segment id")
    return name


@router.get("/api/recordings")
async def recordings(camera: str, date: str, stream: str = "sub") -> dict:
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    if stream not in ("sub", "main"):
        raise HTTPException(400, "stream must be sub or main")
    try:
        day = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc

    start = datetime.combine(day, time(0, 0, 0))
    end = datetime.combine(day, time(23, 59, 59))
    statuses, files = await hub.search_recordings(cam.channel, start, end, stream)

    available = sorted({d.isoformat() for s in statuses for d in s})
    segments = [
        {
            "id": _encode_id(f.file_name),
            "start": int(f.start_time.timestamp() * 1000),
            "end": int(f.end_time.timestamp() * 1000),
            "durationSec": f.duration.total_seconds(),
            "clock": f.start_time.strftime("%H:%M:%S"),
            "cached": clip_cache.is_ready(camera, stream, f.file_name),
            "bytes": f.size,
        }
        for f in files
    ]
    segments.sort(key=lambda s: s["start"])
    return {
        "date": date,
        "stream": stream,
        "availableDates": available,
        "segments": segments,
    }


@router.get("/api/clip")
async def clip(camera: str, id: str, stream: str = "sub"):
    """Serve a cached, seekable segment, or 202 while it's being fetched.

    Returns 200 + the MP4 (FileResponse handles Range) when cached; 202 with
    {status:"caching"} otherwise, having kicked off the download. The frontend
    polls until it gets 200.
    """
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    if stream not in ("sub", "main", "hd"):
        raise HTTPException(400, "stream must be sub, main or hd")
    file_name = _decode_id(id)

    path = clip_cache.path_if_ready(camera, stream, file_name)
    if path is not None:
        return FileResponse(path, media_type="video/mp4")

    err = clip_cache.recent_error(camera, stream, file_name)
    if err is not None:
        # The Hub failed/stalled recently; tell the client instead of spinning.
        return JSONResponse({"status": "error", "detail": err}, status_code=504)

    # User-facing playback: priority over the background mirror.
    clip_cache.ensure(camera, cam.channel, stream, file_name, interactive=True)
    downloaded = clip_cache.progress_bytes(camera, stream, file_name)
    return JSONResponse({"status": "caching", "bytes": downloaded}, status_code=202)


@router.get("/api/bcplay")
async def bcplay_endpoint(camera: str, t: int, stream: str = "main", hevc: int = 1):
    """Open a persistent Baichuan playback stream as fragmented MP4 (one long
    HTTP response). Seeking is done in place via /api/bcplay/seek, so the browser
    keeps this one stream open (app/Nest-style).

    t = epoch ms to start from. stream sub|main. hevc=1 → native HEVC passthrough,
    hevc=0 → H.264 transcode.
    """
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    stream_kind = "subStream" if stream == "sub" else "mainStream"
    gen = bcplay.open_stream(cam.channel, t, stream_kind, transcode=(hevc == 0))
    return StreamingResponse(gen, media_type="video/mp4")


@router.get("/api/bcplay/seek")
async def bcplay_seek(t: int) -> JSONResponse:
    """Reposition the active playback stream in place (epoch ms)."""
    ok = await bcplay.seek_stream(t)
    return JSONResponse({"ok": ok})


@router.get("/api/prefetch")
async def prefetch(camera: str, ids: str, stream: str = "sub") -> dict:
    """Warm the cache for a comma-separated list of segment ids (best-effort)."""
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    scheduled = 0
    for token in filter(None, ids.split(",")):
        try:
            file_name = _decode_id(token)
        except HTTPException:
            continue
        if clip_cache.ensure(camera, cam.channel, stream, file_name) is not None:
            scheduled += 1
    return {"scheduled": scheduled}
