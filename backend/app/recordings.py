"""Recording index + clip/thumbnail endpoints (NAS-backed).

  GET /api/recordings?camera=<slug>&date=YYYY-MM-DD
      -> { date, availableDates, segments:[{id,start,end,durationSec,clock,cached,bytes}] }

  GET /api/clip?camera=<slug>&id=<token>&hevc=1|0
      -> the seekable MP4 for one segment (FileResponse forwards HTTP Range), or
         202 {status:"caching"} while it's being remuxed from the NAS source.

  GET /api/thumbnail?camera=<slug>&t=<epoch-ms>
      -> the per-minute JPEG snapshot nearest wall-clock t (scrub preview).

Recorded video is read entirely from the NAS share (see nas.py); the Hub is used
only for the live view and the camera list.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse

from .hub import hub
from .nas import nas_lib

router = APIRouter()


def _mode(hevc: int) -> str:
    return "hevc" if hevc else "h264"


@router.get("/api/recordings")
async def recordings(camera: str, date: str, stream: str = "main") -> dict:
    # `stream` is accepted for backward compat but ignored: the NAS holds a single
    # 4K main-stream recording per segment.
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError as exc:
        raise HTTPException(400, "date must be YYYY-MM-DD") from exc

    return {
        "date": date,
        "availableDates": nas_lib.available_dates(),
        "segments": nas_lib.segments(camera, cam.channel, date),
    }


@router.get("/api/clip")
async def clip(camera: str, id: str, hevc: int = 1):
    """Serve a cached, seekable segment, or 202 while it's being remuxed.

    Returns 200 + the MP4 (FileResponse handles Range) when ready; 202 with
    {status:"caching"} otherwise, having kicked off the remux. The frontend polls
    until it gets 200.
    """
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    mode = _mode(hevc)
    try:
        path = nas_lib.path_if_ready(camera, id, mode)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if path is not None:
        return FileResponse(path, media_type="video/mp4")

    err = nas_lib.recent_error(camera, id, mode)
    if err is not None:
        return JSONResponse({"status": "error", "detail": err}, status_code=504)

    nas_lib.ensure(camera, id, mode, interactive=True)
    return JSONResponse(
        {"status": "caching", "bytes": nas_lib.progress_bytes(camera, id, mode)},
        status_code=202,
    )


@router.get("/api/thumbnail")
async def thumbnail(camera: str, t: int):
    """The per-minute JPEG snapshot nearest epoch-ms `t` (scrub preview)."""
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    p = nas_lib.nearest_jpeg(cam.channel, t)
    if p is None:
        raise HTTPException(404, "no snapshot")
    return FileResponse(p, media_type="image/jpeg")


@router.get("/api/prefetch")
async def prefetch(camera: str, ids: str, hevc: int = 1) -> dict:
    """Warm the remux cache for a comma-separated list of segment ids."""
    cam = hub.camera_by_slug(camera)
    if cam is None:
        raise HTTPException(404, "unknown camera")
    mode = _mode(hevc)
    scheduled = 0
    for token in filter(None, ids.split(",")):
        try:
            if nas_lib.ensure(camera, token, mode) is not None:
                scheduled += 1
        except ValueError:
            continue
    return {"scheduled": scheduled}
