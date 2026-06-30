"""Thin async wrapper around reolink_aio for our app's needs.

Keeps a single logged-in Host connection and exposes the camera list plus
RTSP sources. Recording search / playback will be added in Phase 3.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from reolink_aio.api import Host
from reolink_aio.enums import VodRequestType
from reolink_aio.typings import VOD_file

from .config import settings


@dataclass
class Camera:
    channel: int
    name: str
    online: bool
    # Stable URL-safe id used in API routes / go2rtc stream names.
    slug: str


def _slugify(name: str, channel: int) -> str:
    safe = "".join(c.lower() if c.isalnum() else "_" for c in name).strip("_")
    safe = "_".join(filter(None, safe.split("_")))  # collapse repeats
    return safe or f"cam{channel}"


class HubManager:
    """Owns the reolink_aio Host and refreshes camera state."""

    def __init__(self) -> None:
        self._host: Host | None = None
        self._cameras: list[Camera] = []
        self._lock = asyncio.Lock()
        self._direct = False  # direct-to-camera mode (no Hub); see settings.camera_host

    @property
    def host(self) -> Host:
        if self._host is None:
            raise RuntimeError("HubManager not connected; call connect() first")
        return self._host

    @property
    def cameras(self) -> list[Camera]:
        return self._cameras

    def camera_by_slug(self, slug: str) -> Camera | None:
        return next((c for c in self._cameras if c.slug == slug), None)

    async def connect(self) -> None:
        async with self._lock:
            if settings.camera_host:
                # Direct-to-camera: no Hub connection. Define the camera from config;
                # RTSP is built straight from its IP in rtsp_url().
                self._direct = True
                self._cameras = [
                    Camera(
                        channel=0,
                        name=settings.camera_name,
                        online=True,
                        slug=_slugify(settings.camera_name, 0),
                    )
                ]
                return
            host = Host(
                settings.host,
                settings.username,
                settings.password,
                port=settings.port,
                use_https=settings.use_https,
            )
            await host.get_host_data()
            await host.get_states()
            self._host = host
            await self._refresh_cameras()

    async def _refresh_cameras(self) -> None:
        host = self.host
        cams: list[Camera] = []
        seen_slugs: set[str] = set()
        for ch in host.channels:
            name = host.camera_name(ch) or f"Camera {ch}"
            slug = _slugify(name, ch)
            # Guarantee uniqueness if two cameras share a name.
            base = slug
            i = 2
            while slug in seen_slugs:
                slug = f"{base}_{i}"
                i += 1
            seen_slugs.add(slug)
            cams.append(
                Camera(
                    channel=ch,
                    name=name,
                    online=host.camera_online(ch),
                    slug=slug,
                )
            )
        self._cameras = cams

    async def refresh_states(self) -> None:
        if self._direct:
            return
        async with self._lock:
            await self.host.get_states()
            await self._refresh_cameras()

    async def rtsp_url(self, channel: int, stream: str) -> str | None:
        """stream is 'main' or 'sub'. Returns full rtsp:// URL with creds."""
        if self._direct:
            # Reolink single-camera RTSP paths are 1-indexed (Preview_01_*).
            kind = "main" if stream == "main" else "sub"
            return (
                f"rtsp://{settings.username}:{settings.password}"
                f"@{settings.camera_host}:554/Preview_{channel + 1:02d}_{kind}"
            )
        return await self.host.get_rtsp_stream_source(channel, stream)

    async def search_recordings(
        self, channel: int, start: datetime, end: datetime, stream: str
    ) -> tuple[list, list[VOD_file]]:
        """Search recordings in [start, end] (naive, Hub-local time).

        Returns (statuses, files). statuses contain the set of dates that have
        recordings; files are the individual segments.
        """
        async with self._lock:
            return await self.host.request_vod_files(
                channel, start, end, stream=stream
            )

    async def download_url(
        self, channel: int, file_name: str, stream: str
    ) -> tuple[str, str]:
        """Fresh (mime, url) to download/decrypt a recording segment as MP4.

        The URL carries a short-lived auth token, so fetch it per request.
        """
        async with self._lock:
            return await self.host.get_vod_source(
                channel, file_name, stream, VodRequestType.DOWNLOAD
            )

    async def close(self) -> None:
        if self._direct:
            self._host = None
            return
        if self._host is not None:
            try:
                await self._host.logout()
            finally:
                self._host = None


hub = HubManager()
