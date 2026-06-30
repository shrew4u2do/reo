"""Local segment cache for recorded playback.

The Hub serves recordings slowly (~38 KB/s sub, ~200 KB/s main) and without HTTP
Range support, so we can't seek a live proxy. Instead we download each segment
once to disk and serve it locally with full Range/seek support.

Streams:
  - "sub":  remuxed to faststart MP4 (already H.264 640x360) — the scrub layer.
  - "hd":   downloads the MAIN source (4K H.265, unplayable in browsers) and
            transcodes to 1080p H.264 — the on-settle quality upgrade.

Scheduling: interactive requests (actual playback) use a separate slot from the
background mirror and pause the mirror while active, so a user-facing fetch is
never stuck behind hundreds of queued mirror downloads. The Hub falls over under
many simultaneous requests, so total concurrency stays at 2.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .go2rtc import _find_ffmpeg
from .hub import hub

_LOG = logging.getLogger("reo.cache")

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "clips"

# If the Hub sends no bytes for this long, treat the download as stalled.
_STALL_TIMEOUT_S = 30.0
# After a failure, don't retry the same segment for this long (avoid hammering a
# Hub that's fallen over).
_ERROR_COOLDOWN_S = 20.0

# e.g. RecS04_DST20260628_113208_113708_...  ->  date 2026-06-28, start 11:32:08
_NAME_RE = re.compile(r"Rec\w{3}(?:_|_DST)(\d{8})_(\d{6})_")

# "hd" is a derived stream: download the main source, transcode to H.264.
_SOURCE_STREAM = {"hd": "main"}


def _key(camera: str, stream: str, file_name: str) -> str:
    return hashlib.sha1(f"{camera}|{stream}|{file_name}".encode()).hexdigest()[:20]


def recording_date(file_name: str) -> str:
    """Parse the recording date (YYYY-MM-DD) out of a Reolink file name."""
    m = _NAME_RE.search(file_name)
    if not m:
        return "unknown"
    d = m.group(1)
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


class ClipCache:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._errors: dict[str, tuple[float, str]] = {}  # key -> (time, message)
        self._bg_sem = asyncio.Semaphore(1)
        self._io_sem = asyncio.Semaphore(1)
        self._io_active = 0
        self._no_io = asyncio.Event()
        self._no_io.set()
        # Held (cleared) while a Baichuan replay stream is open, so the background
        # mirror starts no new Hub downloads and leaves the bandwidth to playback.
        self._play_ok = asyncio.Event()
        self._play_ok.set()
        # Circuit breaker: after repeated background failures, stop hammering the
        # Hub so it can recover. Interactive (user) downloads ignore the breaker.
        self._bg_failures = 0
        self._breaker_until = 0.0
        self._last_interactive = 0.0

    def breaker_open(self) -> bool:
        return time.time() < self._breaker_until

    def recently_active(self, window: float = 15.0) -> bool:
        """True if a user-facing (interactive) download ran recently — the mirror
        backs off so the Hub has spare capacity for scrubbing/playback."""
        return time.time() - self._last_interactive < window

    def hold_background(self, hold: bool) -> None:
        """Pause (hold=True) or resume (hold=False) background mirror downloads
        while a replay stream is open, so the fragile Hub serves playback first."""
        if hold:
            self._play_ok.clear()
        else:
            self._play_ok.set()

    def progress_bytes(self, camera: str, stream: str, file_name: str) -> int:
        """Bytes downloaded so far for an in-progress segment (0 if none)."""
        part = self._path(camera, stream, file_name).with_suffix(".raw.part")
        try:
            return part.stat().st_size
        except OSError:
            return 0

    def recent_error(self, camera: str, stream: str, file_name: str) -> str | None:
        """Return a recent download error for this segment, if within cooldown."""
        rec = self._errors.get(_key(camera, stream, file_name))
        if rec and time.time() - rec[0] < _ERROR_COOLDOWN_S:
            return rec[1]
        return None

    def _path(self, camera: str, stream: str, file_name: str) -> Path:
        # Organized by recording date so retention can drop whole days exactly.
        day = recording_date(file_name)
        return CACHE_DIR / camera / stream / day / f"{_key(camera, stream, file_name)}.mp4"

    def is_ready(self, camera: str, stream: str, file_name: str) -> bool:
        return self._path(camera, stream, file_name).exists()

    def path_if_ready(self, camera: str, stream: str, file_name: str) -> Path | None:
        p = self._path(camera, stream, file_name)
        return p if p.exists() else None

    def ensure(
        self,
        camera: str,
        channel: int,
        stream: str,
        file_name: str,
        interactive: bool = False,
    ) -> asyncio.Task | None:
        """Start caching a segment if not already cached/in-flight.

        Returns the in-flight Task (so callers can await it) or None if already
        cached. `interactive=True` gives the download priority over the mirror.
        """
        if self.is_ready(camera, stream, file_name):
            return None
        # While the breaker is open, skip background work (let the Hub recover).
        if not interactive and self.breaker_open():
            return None
        key = _key(camera, stream, file_name)
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(
                self._download(camera, channel, stream, file_name, interactive)
            )
            self._tasks[key] = task
        return task

    def mirror_paused(self) -> bool:
        return self._io_active > 0

    async def _download(
        self, camera: str, channel: int, stream: str, file_name: str, interactive: bool
    ) -> None:
        key = _key(camera, stream, file_name)
        dest = self._path(camera, stream, file_name)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_raw = dest.with_suffix(".raw.part")
        tmp_out = dest.with_suffix(".out.part")
        src_stream = _SOURCE_STREAM.get(stream, stream)

        if interactive:
            self._io_active += 1
            self._last_interactive = time.time()
            self._no_io.clear()
        try:
            if not interactive:
                # Yield Hub bandwidth to any active interactive download AND to an
                # open replay stream (held until playback closes).
                await self._no_io.wait()
                await self._play_ok.wait()
            sem = self._io_sem if interactive else self._bg_sem
            async with sem:
                if dest.exists():
                    return
                try:
                    url = await asyncio.wait_for(
                        hub.download_url(channel, file_name, src_stream), timeout=20.0
                    )
                    url = url[1] if isinstance(url, tuple) else url
                    await self._curl_download(url, tmp_raw)
                    self._errors.pop(key, None)  # success clears any prior error
                    if not interactive:
                        self._bg_failures = 0  # healthy background download
                except Exception as exc:  # noqa: BLE001
                    tmp_raw.unlink(missing_ok=True)
                    self._errors[key] = (time.time(), f"{type(exc).__name__}: {exc}")
                    _LOG.warning("download failed %s %s: %s", stream, file_name[-40:], exc)
                    if not interactive:
                        self._bg_failures += 1
                        if self._bg_failures >= 3:
                            self._breaker_until = time.time() + 300  # 5-min cooldown
                            self._bg_failures = 0
                            _LOG.warning("mirror circuit breaker OPEN (Hub overloaded) — pausing 5 min")
                    raise
        finally:
            if interactive:
                self._io_active -= 1
                self._last_interactive = time.time()  # start the back-off window now
                if self._io_active == 0:
                    self._no_io.set()

        # Process outside the download slot (CPU work, no Hub involvement).
        if stream == "hd":
            ok = await self._transcode_hd(tmp_raw, tmp_out)
        elif stream == "main":
            # Native 4K H.265 for browsers that support it; tag hvc1 for Chrome.
            ok = await self._remux(tmp_raw, tmp_out, video_tag="hvc1")
        else:
            ok = await self._remux(tmp_raw, tmp_out)
        if ok:
            tmp_raw.unlink(missing_ok=True)
            tmp_out.replace(dest)
        else:
            tmp_out.unlink(missing_ok=True)
            tmp_raw.replace(dest)

    async def _curl_download(self, url: str, dest: Path) -> None:
        """Download via curl with a file-size watchdog.

        curl's own --speed-time doesn't abort a connection that's open but sending
        zero bytes (a fallen-over Hub), so we watch the output file: if it hasn't
        grown for the stall window, we kill curl. curl is a killable subprocess,
        unlike a wedged in-process socket read.
        """
        proc = await asyncio.create_subprocess_exec(
            "curl", "-s", "-k", "--fail", "--connect-timeout", "15",
            "--max-time", "360",
            "-o", str(dest), url,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

        stalled = False

        async def watchdog() -> None:
            # Throughput watchdog: kill if growth over each stall window is below a
            # floor. Catches both dead-stall (0 bytes) and slow-drip (a few KB),
            # which a plain "any growth" check misses.
            nonlocal stalled
            # ~12 KB/s over the window. MUST stay well below the Hub's real sub-stream
            # serving rate (~38-44 KB/s) — a higher floor (the old ~43 KB/s) killed
            # healthy-but-slow sub downloads as "stalled", so the mirror cached nothing.
            min_growth = 360 * 1024
            win_start = time.monotonic()
            win_size = dest.stat().st_size if dest.exists() else 0
            while True:
                await asyncio.sleep(5)
                if time.monotonic() - win_start >= _STALL_TIMEOUT_S:
                    size = dest.stat().st_size if dest.exists() else 0
                    if size - win_size < min_growth:
                        stalled = True
                        try:
                            proc.kill()
                        except ProcessLookupError:
                            pass
                        return
                    win_start, win_size = time.monotonic(), size

        wd = asyncio.create_task(watchdog())
        try:
            rc = await proc.wait()
        finally:
            wd.cancel()
            try:
                await wd
            except asyncio.CancelledError:
                pass
        if stalled:
            raise RuntimeError("Hub download stalled (no data) — overloaded")
        if rc != 0:
            raise RuntimeError(f"curl failed (exit {rc}) — Hub stalled or unreachable")
        if not dest.exists() or dest.stat().st_size == 0:
            raise RuntimeError("download produced no data")

    async def _run_ffmpeg(self, *args: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            _find_ffmpeg(), "-y", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    async def _remux(self, src: Path, dst: Path, video_tag: str | None = None) -> bool:
        # -f mp4 because the temp file's extension isn't a recognized format.
        tag = ["-tag:v", video_tag] if video_tag else []
        ok = await self._run_ffmpeg(
            "-i", str(src), "-c", "copy", *tag, "-movflags", "+faststart", "-f", "mp4", str(dst)
        )
        return ok and dst.exists() and dst.stat().st_size > 0

    async def _transcode_hd(self, src: Path, dst: Path) -> bool:
        # 4K H.265 -> 1080p H.264 (browser-playable, all platforms incl. phones).
        ok = await self._run_ffmpeg(
            "-i", str(src),
            "-vf", "scale=-2:1080",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
            "-c:a", "aac",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(dst),
        )
        return ok and dst.exists() and dst.stat().st_size > 0

    def purge_older_than(self, days: int) -> int:
        """Delete cached date-folders older than `days`. Returns files removed."""
        cutoff: date = (datetime.now() - timedelta(days=days)).date()
        removed = 0
        if not CACHE_DIR.exists():
            return 0
        for cam_dir in CACHE_DIR.iterdir():
            for stream_dir in cam_dir.iterdir() if cam_dir.is_dir() else []:
                for day_dir in stream_dir.iterdir() if stream_dir.is_dir() else []:
                    try:
                        d = datetime.strptime(day_dir.name, "%Y-%m-%d").date()
                    except ValueError:
                        continue
                    if d < cutoff:
                        for f in day_dir.glob("*"):
                            f.unlink(missing_ok=True)
                            removed += 1
                        try:
                            day_dir.rmdir()
                        except OSError:
                            pass
        return removed

    def cached_count(self, camera: str, stream: str, day: str) -> int:
        p = CACHE_DIR / camera / stream / day
        return sum(1 for _ in p.glob("*.mp4")) if p.exists() else 0


clip_cache = ClipCache()
