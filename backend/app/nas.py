"""NAS-backed recorded playback library.

The Hub FTP-uploads its recordings to a NAS share (SMB-mounted, see
`settings.nas_root`), one folder per day:

    <root>/<YYYY-MM-DD>/<Cam Name>_<NN>_<Hub Name>_<YYYYMMDDHHMMSS>.mp4   (~10-min main stream)
    <root>/<YYYY-MM-DD>/<Cam Name>_<NN>_<Hub Name>_<YYYYMMDDHHMMSS>.jpg   (per-minute snapshot)

`NN` is the 2-digit channel; the trailing 14 digits are the segment/clip start.
The MP4s are 4K HEVC + AAC fragmented MP4 (no `sidx`/`mfra`), so a plain `<video>`
can't reliably seek them — we lazily remux each to a normal, indexed MP4 in a local
cache and serve that with full Range/seek support. This replaces the old Baichuan
replay + slow CGI clip cache entirely; the Hub is no longer in the playback path.

Two remux modes:
  - "hevc": stream-copy (`-c copy`) into a faststart MP4, tagged hvc1 — keeps the
            native 4K HEVC for browsers that decode it. Cheap (seconds, local I/O).
  - "h264": transcode to 1080p H.264 for browsers without an HEVC decoder.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import logging
import re
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from .config import settings
from .go2rtc import _find_ffmpeg

_LOG = logging.getLogger("reo.nas")

CACHE_DIR = Path(__file__).resolve().parents[2] / "data" / "clips"

# After a remux failure, don't retry the same clip for this long.
_ERROR_COOLDOWN_S = 20.0

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")  # flat layout folder: 2026-06-30
_YEAR_RE = re.compile(r"^\d{4}$")
_MD_RE = re.compile(r"^\d{2}$")
# A decoded segment id is "<date>/<file>.mp4" — either layout, no path traversal:
#   flat:    2026-06-30/<file>.mp4
#   nested:  2026/06/30/<file>.mp4
_RELPATH_RE = re.compile(r"^(?:\d{4}-\d{2}-\d{2}|\d{4}/\d{2}/\d{2})/[^/\\]+\.mp4$")
# Trailing start timestamp (14 digits) just before the extension.
_TS_RE = re.compile(r"_(\d{14})\.(?:mp4|jpg|jpeg)$", re.IGNORECASE)
# 2-digit channel field (e.g. "_00_"). The timestamp run ends in "." not "_", so
# this never matches inside it.
_CH_RE = re.compile(r"_(\d{2})_")

VALID_MODES = ("hevc", "h264")


def _encode_id(relpath: str) -> str:
    return base64.urlsafe_b64encode(relpath.encode()).decode().rstrip("=")


def _decode_id(token: str) -> str:
    """Decode a segment id back to its "<date>/<file>.mp4" relative path."""
    pad = "=" * (-len(token) % 4)
    try:
        relpath = base64.urlsafe_b64decode(token + pad).decode()
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("bad segment id") from exc
    relpath = relpath.replace("\\", "/")
    if not _RELPATH_RE.match(relpath):
        raise ValueError("invalid segment id")
    return relpath


def _parse(name: str) -> tuple[int, datetime] | None:
    """(channel, start datetime) parsed from a Reolink FTP file name, or None."""
    mt = _TS_RE.search(name)
    mc = _CH_RE.search(name)
    if not mt or not mc:
        return None
    try:
        dt = datetime.strptime(mt.group(1), "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return int(mc.group(1)), dt


def _find_ffprobe() -> str:
    """ffprobe lives alongside ffmpeg (resolved by go2rtc's helper)."""
    p = Path(_find_ffmpeg())
    cand = p.with_name("ffprobe.exe") if p.suffix.lower() == ".exe" else p.with_name("ffprobe")
    return str(cand) if cand.exists() else "ffprobe"


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _key(camera: str, relpath: str, mode: str) -> str:
    return hashlib.sha1(f"{camera}|{mode}|{relpath}".encode()).hexdigest()[:20]


class NasLibrary:
    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}
        self._errors: dict[str, tuple[float, str]] = {}  # key -> (time, message)

    # ---- index ----

    def _root(self) -> Path:
        return Path(settings.nas_root)

    def _date_folders(self, date_str: str) -> list[Path]:
        """Existing on-disk folders for a date, across both layouts (flat + nested)."""
        y, m, d = date_str.split("-")
        root = self._root()
        cands = [root / date_str, root / y / m / d]
        return [p for p in cands if p.is_dir()]

    def available_dates(self) -> list[str]:
        root = self._root()
        if not root.exists():
            return []
        dates: set[str] = set()
        for p in root.iterdir():
            if not p.is_dir():
                continue
            if _DATE_RE.match(p.name):  # flat: <root>/2026-06-30
                dates.add(p.name)
            elif _YEAR_RE.match(p.name):  # nested: <root>/2026/06/30
                for mp in p.iterdir():
                    if mp.is_dir() and _MD_RE.match(mp.name):
                        for dp in mp.iterdir():
                            if dp.is_dir() and _MD_RE.match(dp.name):
                                dates.add(f"{p.name}-{mp.name}-{dp.name}")
        return sorted(dates)

    def segments(self, camera: str, channel: int, date_str: str) -> list[dict]:
        """Timeline segments for one camera/day, built purely from file names.

        Durations come from neighbouring start times (segments are contiguous with
        a slight overlap → no gaps), so we never have to probe each file. The last
        (possibly still-growing) segment ends at its file mtime.
        """
        root = self._root()
        items: list[tuple[datetime, Path, str]] = []  # (start, path, relpath prefix)
        for folder in self._date_folders(date_str):
            rel_prefix = folder.relative_to(root).as_posix()
            for p in folder.glob("*.mp4"):
                parsed = _parse(p.name)
                # Skip 0-byte files (an in-progress/failed FTP upload would otherwise
                # index as a segment and crash the remux).
                if parsed and parsed[0] == channel and _safe_size(p) > 0:
                    items.append((parsed[1], p, rel_prefix))
        items.sort(key=lambda x: x[0])

        segs: list[dict] = []
        for i, (dt, p, rel_prefix) in enumerate(items):
            start_ms = int(dt.timestamp() * 1000)
            try:
                mtime_ms = int(p.stat().st_mtime * 1000)
            except OSError:
                mtime_ms = start_ms
            # End at the file's own finish (mtime) so a recording GAP between sets
            # never renders as one segment spanning it; for contiguous segments the
            # next start is ~mtime, so this also trims the small inter-file overlap.
            end_ms = mtime_ms
            if i + 1 < len(items):
                end_ms = min(end_ms, int(items[i + 1][0].timestamp() * 1000))
            if end_ms <= start_ms:
                end_ms = start_ms + 1000
            relpath = f"{rel_prefix}/{p.name}"
            segs.append(
                {
                    "id": _encode_id(relpath),
                    "start": start_ms,
                    "end": end_ms,
                    "durationSec": (end_ms - start_ms) / 1000,
                    "clock": dt.strftime("%H:%M:%S"),
                    "cached": True,  # the source always exists on the NAS
                    "bytes": _safe_size(p),
                }
            )
        return segs

    def nearest_jpeg(self, channel: int, t_ms: int) -> Path | None:
        """The per-minute snapshot closest to wall-clock t (same day, either layout)."""
        date_str = datetime.fromtimestamp(t_ms / 1000).strftime("%Y-%m-%d")
        best: Path | None = None
        best_d: float | None = None
        for folder in self._date_folders(date_str):
            for p in folder.glob("*.jpg"):
                parsed = _parse(p.name)
                if not parsed or parsed[0] != channel:
                    continue
                d = abs(parsed[1].timestamp() * 1000 - t_ms)
                if best_d is None or d < best_d:
                    best, best_d = p, d
        return best

    # ---- remux cache (id-based) ----

    def _source(self, relpath: str) -> Path | None:
        p = (self._root() / relpath)
        return p if p.exists() else None

    def _cache_path(self, camera: str, relpath: str, mode: str) -> Path:
        # Cache stays organized by flat date regardless of the NAS folder layout.
        head = relpath.rsplit("/", 1)[0]  # "2026-06-30" or "2026/06/30"
        day = head if "-" in head else head.replace("/", "-")
        return CACHE_DIR / camera / mode / day / f"{_key(camera, relpath, mode)}.mp4"

    def is_ready(self, camera: str, token: str, mode: str) -> bool:
        try:
            relpath = _decode_id(token)
        except ValueError:
            return False
        return self._cache_path(camera, relpath, mode).exists()

    def path_if_ready(self, camera: str, token: str, mode: str) -> Path | None:
        relpath = _decode_id(token)
        p = self._cache_path(camera, relpath, mode)
        return p if p.exists() else None

    def progress_bytes(self, camera: str, token: str, mode: str) -> int:
        try:
            relpath = _decode_id(token)
        except ValueError:
            return 0
        part = self._cache_path(camera, relpath, mode).with_suffix(".part")
        return _safe_size(part)

    def recent_error(self, camera: str, token: str, mode: str) -> str | None:
        try:
            relpath = _decode_id(token)
        except ValueError:
            return None
        rec = self._errors.get(_key(camera, relpath, mode))
        if rec and time.time() - rec[0] < _ERROR_COOLDOWN_S:
            return rec[1]
        return None

    def ensure(
        self, camera: str, token: str, mode: str, interactive: bool = False
    ) -> asyncio.Task | None:
        """Start remuxing this segment to the seekable cache if not already done.

        Returns the in-flight Task (callers may await) or None if already cached.
        `interactive` is accepted for parity with the old cache; remux is local and
        cheap, so there's no background throttling to coordinate.
        """
        relpath = _decode_id(token)
        if self._cache_path(camera, relpath, mode).exists():
            return None
        key = _key(camera, relpath, mode)
        task = self._tasks.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._remux(camera, relpath, mode))
            self._tasks[key] = task
        return task

    async def _probe_vcodec(self, src: Path) -> str:
        """Source video codec name (e.g. 'hevc', 'h264'), lowercased; '' on failure."""
        try:
            proc = await asyncio.create_subprocess_exec(
                _find_ffprobe(), "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=codec_name", "-of", "default=nw=1:nk=1",
                str(src),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            out, _ = await proc.communicate()
            return out.decode(errors="ignore").strip().lower()
        except Exception:  # noqa: BLE001
            return ""

    def _ffmpeg_args(self, src: Path, dst: Path, src_is_hevc: bool, mode: str) -> list[str]:
        if src_is_hevc and mode == "h264":
            # 4K HEVC -> 1080p H.264 for browsers without an HEVC decoder.
            return [
                "-i", str(src),
                "-vf", "scale=-2:1080",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
                "-c:a", "aac",
                "-movflags", "+faststart",
                "-f", "mp4", str(dst),
            ]
        if src_is_hevc:
            # Stream-copy to an indexed, seekable MP4; tag hvc1 for browsers/MSE.
            return [
                "-i", str(src),
                "-c", "copy", "-tag:v", "hvc1",
                "-movflags", "+faststart",
                "-f", "mp4", str(dst),
            ]
        # Non-HEVC source (e.g. the H.264 sub stream): copy as-is. H.264 plays
        # natively everywhere, and force-tagging it hvc1 would corrupt it.
        return [
            "-i", str(src),
            "-c", "copy",
            "-movflags", "+faststart",
            "-f", "mp4", str(dst),
        ]

    async def _remux(self, camera: str, relpath: str, mode: str) -> None:
        key = _key(camera, relpath, mode)
        src = self._source(relpath)
        if src is None:
            self._errors[key] = (time.time(), "source not found on NAS")
            return
        dest = self._cache_path(camera, relpath, mode)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".part")
        vcodec = await self._probe_vcodec(src)
        args = self._ffmpeg_args(src, tmp, vcodec in ("hevc", "h265"), mode)
        ok = await self._run_ffmpeg(*args)
        if ok and tmp.exists() and tmp.stat().st_size > 0:
            tmp.replace(dest)
            self._errors.pop(key, None)
        else:
            tmp.unlink(missing_ok=True)
            self._errors[key] = (time.time(), "remux failed")
            _LOG.warning("remux failed (%s) %s", mode, relpath)

    async def _run_ffmpeg(self, *args: str) -> bool:
        proc = await asyncio.create_subprocess_exec(
            _find_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error", *args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
        return proc.returncode == 0

    # ---- retention (local cache only; the NAS is read-only) ----

    def purge_older_than(self, days: int) -> int:
        """Delete cached date-folders older than `days`. Returns files removed."""
        cutoff: date = (datetime.now() - timedelta(days=days)).date()
        removed = 0
        if not CACHE_DIR.exists():
            return 0
        for cam_dir in CACHE_DIR.iterdir():
            for mode_dir in cam_dir.iterdir() if cam_dir.is_dir() else []:
                for day_dir in mode_dir.iterdir() if mode_dir.is_dir() else []:
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


nas_lib = NasLibrary()
