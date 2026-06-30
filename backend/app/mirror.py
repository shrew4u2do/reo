"""Background remux warmer.

The NAS already holds every recording, so there's nothing to *download*. This just
pre-remuxes the most recent few segments of the current day into the local seekable
cache (a cheap local `ffmpeg -c copy`), so scrubbing the live edge is instant
instead of waiting a few seconds for the first remux. A retention loop drops old
entries from the *local cache* — it never touches the NAS (read-only).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from .nas import nas_lib
from .hub import hub

_LOG = logging.getLogger("reo.mirror")

WARM_MODE = "hevc"          # cheap stream-copy; h264 transcode warms on demand only
WARM_COUNT = 6              # most-recent segments to keep warm (~1 hour)
WARM_INTERVAL_S = 120
RETENTION_DAYS = 14
RETENTION_INTERVAL_S = 6 * 3600


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


class MirrorService:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.status: dict = {"warm": None, "lastPurge": None}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._warm_loop(), name="mirror-warm"),
            asyncio.create_task(self._retention_loop(), name="mirror-retention"),
        ]

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks = []

    async def _warm_loop(self) -> None:
        while self._running:
            try:
                day = _today()
                warmed = 0
                for cam in hub.cameras:
                    segs = nas_lib.segments(cam.slug, cam.channel, day)
                    for seg in segs[-WARM_COUNT:]:
                        task = nas_lib.ensure(cam.slug, seg["id"], WARM_MODE)
                        if task is not None:
                            try:
                                await task  # one at a time
                                warmed += 1
                            except Exception:  # noqa: BLE001
                                pass
                self.status["warm"] = (
                    f"{datetime.now().isoformat(timespec='seconds')}: {warmed} remuxed"
                )
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("warm loop error: %s", exc)
            await asyncio.sleep(WARM_INTERVAL_S)

    async def _retention_loop(self) -> None:
        while self._running:
            try:
                removed = nas_lib.purge_older_than(RETENTION_DAYS)
                self.status["lastPurge"] = (
                    f"{datetime.now().isoformat(timespec='seconds')}: removed {removed}"
                )
                if removed:
                    _LOG.info("retention removed %d old cached clips", removed)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("retention error: %s", exc)
            await asyncio.sleep(RETENTION_INTERVAL_S)


mirror = MirrorService()
