"""Background sub-stream mirror.

Continuously copies the lightweight sub stream from the Hub to the local clip
cache so the timeline can scrub the whole retention window instantly. Three
concurrent loops:

  - recent: every couple minutes, cache the last ~30 min (keeps "now" warm)
  - backfill: walk back over the retention window caching everything not yet
    cached (fills history; slow, gated by the cache's download semaphore)
  - retention: drop cached days older than the window

HD (main) is intentionally NOT mirrored — it's fetched on demand when the user
settles on a moment (main downloads at ~2.5x realtime, fine with caching).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta

from .cache import clip_cache
from .hub import hub
from . import bcplay


def _paused() -> bool:
    """Hold off when the Hub is overloaded, a replay is playing, OR the user was
    recently scrubbing/playing — leave the fragile Hub spare capacity for them."""
    return (
        clip_cache.breaker_open()
        or bcplay.playback_active()
        or clip_cache.recently_active()
    )

_LOG = logging.getLogger("reo.mirror")

STREAM = "sub"
RETENTION_DAYS = 14
BACKFILL_DAYS = 3  # actively pre-cache only the last few days (recent = scrubbed)
RECENT_INTERVAL_S = 120
RECENT_WINDOW_MIN = 30
BACKFILL_PASS_PAUSE_S = 3600  # wait between full backfill passes
RETENTION_INTERVAL_S = 6 * 3600
MIRROR_GAP_S = 10  # pause between background segment downloads (gentle on the Hub)


class MirrorService:
    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._running = False
        self.status: dict = {"recent": None, "backfill": None, "lastPurge": None}

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._tasks = [
            asyncio.create_task(self._recent_loop(), name="mirror-recent"),
            asyncio.create_task(self._backfill_loop(), name="mirror-backfill"),
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

    async def _ensure_range(self, channel: int, camera: str, start: datetime, end: datetime) -> int:
        """Search [start,end] and cache each segment one at a time, with a gap
        between downloads so we don't overwhelm the (fragile) Hub. Returns the
        number of segments seen."""
        try:
            _statuses, files = await hub.search_recordings(channel, start, end, STREAM)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("search failed %s..%s: %s", start, end, exc)
            return 0
        for f in reversed(files):  # newest first — recent footage is scrubbed most
            if not self._running or _paused():
                break
            task = clip_cache.ensure(camera, channel, STREAM, f.file_name)
            if task is not None:
                try:
                    await task  # one segment at a time
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(MIRROR_GAP_S)  # let the Hub breathe
        return len(files)

    async def _recent_loop(self) -> None:
        while self._running:
            if _paused():
                await asyncio.sleep(60)
                continue
            try:
                now = datetime.now()
                start = now - timedelta(minutes=RECENT_WINDOW_MIN)
                for cam in hub.cameras:
                    await self._ensure_range(cam.channel, cam.slug, start, now + timedelta(minutes=2))
                self.status["recent"] = now.isoformat(timespec="seconds")
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("recent loop error: %s", exc)
            await asyncio.sleep(RECENT_INTERVAL_S)

    async def _backfill_loop(self) -> None:
        # Let recent-tail get a head start before hammering the Hub with history.
        await asyncio.sleep(15)
        while self._running:
            for offset in range(0, BACKFILL_DAYS):
                if not self._running:
                    return
                while _paused() and self._running:
                    await asyncio.sleep(30)
                day = (datetime.now() - timedelta(days=offset)).date()
                for cam in hub.cameras:
                    start = datetime.combine(day, time(0, 0, 0))
                    end = datetime.combine(day, time(23, 59, 59))
                    n = await self._ensure_range(cam.channel, cam.slug, start, end)
                    cached = clip_cache.cached_count(cam.slug, STREAM, day.isoformat())
                    self.status["backfill"] = (
                        f"{cam.slug} {day.isoformat()}: {cached}/{n} cached"
                    )
                    _LOG.info("backfill %s", self.status["backfill"])
            await asyncio.sleep(BACKFILL_PASS_PAUSE_S)

    async def _retention_loop(self) -> None:
        while self._running:
            try:
                removed = clip_cache.purge_older_than(RETENTION_DAYS)
                self.status["lastPurge"] = (
                    f"{datetime.now().isoformat(timespec='seconds')}: removed {removed}"
                )
                if removed:
                    _LOG.info("retention removed %d old clips", removed)
            except Exception as exc:  # noqa: BLE001
                _LOG.warning("retention error: %s", exc)
            await asyncio.sleep(RETENTION_INTERVAL_S)


mirror = MirrorService()
