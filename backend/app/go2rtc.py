"""Generate go2rtc config from the Hub's cameras and manage the subprocess.

go2rtc does the heavy lifting for live view: it pulls each camera's RTSP feed
and republishes it as low-latency WebRTC (and HLS/MSE) to browsers. The sub
stream is H.264 (universally WebRTC-compatible); the main stream may be H.265,
so we expose a transcoded H.264 variant for the HD view.
"""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

import yaml

from .hub import hub


def _find_ffmpeg() -> str:
    """Resolve ffmpeg robustly, independent of the launching shell's PATH.

    Falls back to common Windows winget install locations so the backend works
    even when started from a shell with a stale PATH.
    """
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates: list[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        base = Path(local) / "Microsoft" / "WinGet"
        candidates.append(base / "Links" / "ffmpeg.exe")
        candidates.extend((base / "Packages").glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"))
    for c in candidates:
        if c.exists():
            return str(c)
    return "ffmpeg"  # last resort; go2rtc will error visibly if truly missing

BIN_DIR = Path(__file__).resolve().parents[2] / "bin"
GO2RTC_EXE = BIN_DIR / ("go2rtc.exe" if (BIN_DIR / "go2rtc.exe").exists() else "go2rtc")
CONFIG_PATH = BIN_DIR / "go2rtc.gen.yaml"

API_PORT = 1984
WEBRTC_PORT = 8555


class Go2rtcManager:
    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None

    async def build_config(self) -> dict:
        ffmpeg_bin = _find_ffmpeg()
        streams: dict[str, object] = {}
        for cam in hub.cameras:
            main = await hub.rtsp_url(cam.channel, "main")
            sub = await hub.rtsp_url(cam.channel, "sub")
            if sub:
                streams[f"{cam.slug}_sub"] = sub
            if main:
                # Primary source raw; second entry is an on-demand H.264
                # transcode for browsers that can't do H.265 over WebRTC.
                streams[cam.slug] = [
                    main,
                    f"ffmpeg:{cam.slug}#video=h264#hardware",
                ]
        return {
            # origin:* allows the frontend (served from a different port/host)
            # to open WebRTC WebSockets; go2rtc 1.9+ blocks cross-origin by default.
            "api": {"listen": "0.0.0.0:%d" % API_PORT, "origin": "*"},
            "rtsp": {"listen": ":8554"},
            "webrtc": {"listen": ":%d" % WEBRTC_PORT},
            "ffmpeg": {"bin": ffmpeg_bin},
            "log": {"level": "info"},
            "streams": streams,
        }

    async def write_config(self) -> Path:
        cfg = await self.build_config()
        CONFIG_PATH.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        return CONFIG_PATH

    async def start(self) -> None:
        await self.write_config()
        self._proc = await asyncio.create_subprocess_exec(
            str(GO2RTC_EXE),
            "-config",
            str(CONFIG_PATH),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )

    async def stop(self) -> None:
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self._proc.kill()
        self._proc = None

    @property
    def api_port(self) -> int:
        return API_PORT


go2rtc = Go2rtcManager()
