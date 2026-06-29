"""FastAPI application: connects the Hub, runs go2rtc, serves the API.

Phase 2 surface:
  GET /api/health           - liveness + hub/go2rtc status
  GET /api/cameras          - camera list with go2rtc stream names
  GET /api/info             - ports the frontend needs (go2rtc WebRTC)
Recording search/playback endpoints arrive in Phase 3.
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import bcplay
from .go2rtc import go2rtc
from .hub import hub
from .mirror import mirror
from .recordings import router as recordings_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    await hub.connect()
    await go2rtc.start()
    # Gentle sub-stream mirror: pre-caches recent days' low-res clips for instant
    # timeline scrubbing. Backs off whenever the user is active (recently_active /
    # playback_active) and trips a circuit breaker if the Hub struggles.
    mirror.start()
    await bcplay._replenish_spare()  # pre-warm a replay connection for fast first seek
    try:
        yield
    finally:
        await mirror.stop()
        await go2rtc.stop()
        await hub.close()


app = FastAPI(title="reo", lifespan=lifespan)

# Dev: the Vite dev server runs on a different origin. Lock down later.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


app.include_router(recordings_router)


@app.get("/api/health")
async def health() -> dict:
    return {
        "ok": True,
        "cameras": len(hub.cameras),
    }


@app.get("/api/mirror")
async def mirror_status() -> dict:
    return mirror.status


@app.get("/api/info")
async def info() -> dict:
    # The frontend connects to go2rtc on the same hostname it used to reach us.
    return {"go2rtcPort": go2rtc.api_port}


@app.get("/api/cameras")
async def cameras() -> list[dict]:
    out = []
    for cam in hub.cameras:
        out.append(
            {
                "slug": cam.slug,
                "name": cam.name,
                "channel": cam.channel,
                "online": cam.online,
                # go2rtc stream names (see go2rtc.py).
                "streams": {"hd": cam.slug, "sd": f"{cam.slug}_sub"},
            }
        )
    return out
