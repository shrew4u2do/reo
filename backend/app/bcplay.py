"""Persistent Baichuan replay with in-place seeking (the Reolink-app model).

ONE replay stays open on the single Hub connection; the browser holds ONE
fragmented-MP4 stream (MSE), and seeks reposition the live replay with cmd 123
(no stop/restart, no new connection — fast and gentle on the session-limited
Hub). The socket tee forwards only command frames to reolink_aio and consumes
the media (class 416a) itself, so command sends stay in sync amid the media.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime

from .cache import clip_cache
from .config import settings
from .go2rtc import _find_ffmpeg
from .hub import hub
from . import bc_replay, bc_demux

_LOG = logging.getLogger("reo.bcplay")
_lock = asyncio.Lock()
_stream: "PlaybackStream | None" = None
_playing_until = 0.0  # grace-window cooldown after a stream closes
_PLAYBACK_GRACE_S = 20.0


def playback_active() -> bool:
    """True while a replay stream is open — even if its media has STALLED — plus a
    short grace window after it closes. The mirror keys its back-off on this, so it
    yields the Hub to playback for the whole session, not just while bytes flow.
    (The old version only marked activity per emitted chunk, so a stuck seek let the
    mirror resume after 10s and pile more load onto an already-wedged Hub.)"""
    s = _stream
    if s is not None and not s.stopped:
        return True
    return time.time() < _playing_until


async def _replenish_spare() -> None:  # kept for main.py startup hook
    return


# Per-day file-list cache (skip cmd 14/15 + HTTP search on every seek).
_files_cache: dict = {}


async def _list_files_cached(host, channel: int, when: datetime, stream_kind: str):
    key = (channel, when.strftime("%Y-%m-%d"), stream_kind)
    hit = _files_cache.get(key)
    is_today = when.date() == datetime.now().date()
    if hit is not None and (not is_today or time.time() - hit[2] < 60):
        return hit[0], hit[1]
    uid, files = await bc_replay.list_files(host, channel, when, stream=stream_kind)
    if files:
        _files_cache[key] = (uid, files, time.time())
    return uid, files


def _secs(d: dict) -> int:
    return d["hour"] * 3600 + d["minute"] * 60 + d["second"]


def _seektime(when: datetime) -> dict:
    return {"year": when.year, "month": when.month, "day": when.day,
            "hour": when.hour, "minute": when.minute, "second": when.second}


# --- Permanent media filter on the Hub connection ---
# reolink_aio's command parser desyncs the moment it sees replay media (class
# 416a). We install ONE filter on the socket that strips media — routing it to
# the active stream's sink — and forwards only command frames to reolink_aio.
# It stays installed for the connection's life, so command sends (stop/seek/
# start) never desync, even between streams.
_filter_framer = bc_demux.BaichuanFramer()
_real_dr = None
_media_sink = None  # callable(body: bytes, poff: int) | None


def _install_filter() -> None:
    global _real_dr
    proto = hub.host.baichuan._connection._protocol
    if getattr(proto, "_reo_filtered", False):
        return
    _real_dr = proto.data_received

    def filtered(data: bytes):
        keep = bytearray()
        try:
            for cmd_id, mclass, poff, body, raw in _filter_framer.feed(data):
                # Replay media arrives as cmd 5 with a non-zero message class; the
                # Hub has tagged it both "416a" and "436a" across firmware — route
                # either to the active stream. Acks/command replies are class 0000
                # and must pass through to reolink_aio.
                if cmd_id == 5 and mclass in ("416a", "436a"):
                    sink = _media_sink
                    if sink is not None:
                        sink(body, poff)
                else:
                    keep += raw
        except Exception as exc:  # never break the real connection
            _LOG.debug("filter error: %s", exc)
        if keep:
            _real_dr(bytes(keep))

    proto.data_received = filtered
    proto._reo_filtered = True


class PlaybackStream:
    def __init__(self, channel: int, stream_kind: str, transcode: bool) -> None:
        self.channel = channel
        self.stream_kind = stream_kind
        self.transcode = transcode
        self.host = hub.host
        self.ffmpeg: asyncio.subprocess.Process | None = None
        self.key = b""
        self.queue: asyncio.Queue = asyncio.Queue()
        self.feeder: asyncio.Task | None = None
        self.demux = bc_demux.BcMediaStreamDemuxer()
        self._sink = self._ingest  # stable bound ref for sink identity checks
        self.uid = ""
        self.files: list = []
        self.cur_fid: str | None = None
        self.stopped = False

    def _file_for(self, when: datetime):
        target = _secs(_seektime(when))
        chosen = self.files[0]
        for fid, st in self.files:
            if _secs(st) <= target:
                chosen = (fid, st)
            else:
                break
        return chosen

    async def open(self, t_ms: int) -> None:
        clip_cache.hold_background(True)  # mirror yields the Hub while we play
        when = datetime.fromtimestamp(t_ms / 1000)
        self.uid, self.files = await _list_files_cached(self.host, self.channel, when, self.stream_kind)
        if not self.files:
            raise RuntimeError("no recordings for that day")

        vargs = (["-c:v", "copy", "-tag:v", "hvc1"] if not self.transcode
                 else ["-vf", "scale=-2:1080", "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency"])
        self.ffmpeg = await asyncio.create_subprocess_exec(
            _find_ffmpeg(), "-hide_banner", "-loglevel", "error",
            "-fflags", "+genpts+nobuffer+discardcorrupt", "-flags", "low_delay",
            "-probesize", "50000", "-analyzeduration", "0",
            "-f", "hevc", "-i", "pipe:0",
            *vargs,
            "-movflags", "frag_keyframe+empty_moov+default_base_moof",
            "-flush_packets", "1", "-f", "mp4", "pipe:1",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        )

        global _media_sink
        self.key = self.host.baichuan._aes_key
        _install_filter()  # permanent media filter (idempotent)
        self.feeder = asyncio.create_task(self._feed())

        fid, st = self._file_for(when)
        self.cur_fid = fid
        # Stop any prior replay FIRST (reliable: the filter keeps reolink_aio in
        # sync so this command lands), then route media to THIS stream and start.
        await bc_replay.stop_replay(self.host)
        _media_sink = self._sink
        # Start at the FILE START — a continuous stream from the segment boundary.
        # (Repositioning mid-stream freezes playback: with hvc1 the codec params
        # live in the init segment, so a post-seek GOP's new params are lost.)
        await bc_replay.start_replay(self.host, self.channel, self.uid, fid,
                                     st, stream=self.stream_kind)

    def _ingest(self, body: bytes, poff: int) -> None:
        try:
            h265 = self.demux.feed(bc_demux.media_from_message(body, poff, self.key))
            if h265:
                self.queue.put_nowait(h265)
        except Exception as exc:  # noqa: BLE001
            _LOG.debug("ingest error: %s", exc)

    async def _feed(self) -> None:
        while True:
            chunk = await self.queue.get()
            if chunk is None or self.ffmpeg is None or self.ffmpeg.stdin is None:
                break
            try:
                self.ffmpeg.stdin.write(chunk)
                await self.ffmpeg.stdin.drain()
            except Exception:
                break

    async def seek(self, t_ms: int) -> bool:
        """In-place repositioning is disabled: cmd 123 streams new media but the
        browser freezes on the codec discontinuity (hvc1 init-segment params).
        Always reopen instead — return False so the caller restarts the stream."""
        return False

    async def output(self):
        global _playing_until
        try:
            while True:
                chunk = await self.ffmpeg.stdout.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            _playing_until = time.time() + _PLAYBACK_GRACE_S  # cooldown after close
            await self.close()

    async def close(self) -> None:
        if self.stopped:
            return
        self.stopped = True
        clip_cache.hold_background(False)  # release the mirror (grace window applies)
        global _media_sink
        if _media_sink is self._sink:  # only if a newer stream hasn't taken over
            _media_sink = None
        if self.feeder is not None:
            self.feeder.cancel()
        if self.ffmpeg and self.ffmpeg.returncode is None:
            try:
                self.ffmpeg.kill()
            except Exception:
                pass
        # Best-effort stop on the Hub (the permanent filter keeps reolink_aio in
        # sync, and the next open's stop_replay is a reliable backstop).
        try:
            await bc_replay.stop_replay(self.host)
        except Exception:
            pass


async def open_stream(channel: int, t_ms: int, stream_kind: str, transcode: bool):
    """Open a fresh persistent stream (closing any prior) and yield its fMP4."""
    global _stream
    async with _lock:
        if _stream is not None:
            await _stream.close()
        s = PlaybackStream(channel, stream_kind, transcode)
        _stream = s
        try:
            await s.open(t_ms)
        except Exception:
            _LOG.warning("replay open failed", exc_info=True)
            await s.close()  # don't leak an "active" stream that holds the mirror
            raise
    async for chunk in s.output():
        yield chunk


async def seek_stream(t_ms: int) -> bool:
    """Reposition the active stream in place. Returns True only if handled
    in-place (same file); False means the caller should reopen the stream."""
    s = _stream
    if s is None or s.stopped:
        return False
    return await s.seek(t_ms)
