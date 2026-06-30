"""Configuration loaded from environment / .env file."""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _bool(val: str | None, default: bool = True) -> bool:
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    host: str = os.getenv("REOLINK_HOST", "")
    port: int = int(os.getenv("REOLINK_PORT", "443"))
    username: str = os.getenv("REOLINK_USERNAME", "admin")
    password: str = os.getenv("REOLINK_PASSWORD", "")
    use_https: bool = _bool(os.getenv("REOLINK_USE_HTTPS"), True)
    # Direct-to-camera mode: when REOLINK_CAMERA_HOST is set, the app skips the Hub
    # entirely for live — go2rtc pulls RTSP straight from the camera's IP and the
    # camera list is defined here (the camera must have RTSP enabled / be unpaired
    # from the Hub). Leave blank to use the Hub as the RTSP source + camera list.
    camera_host: str = os.getenv("REOLINK_CAMERA_HOST", "")
    camera_name: str = os.getenv("REOLINK_CAMERA_NAME", "Front Door")
    # Root of the NAS share the Hub FTP-uploads recordings to (SMB-mounted). Holds
    # one folder per day (<root>/<YYYY-MM-DD>/) of ~10-min main-stream MP4 segments
    # plus per-minute JPEG snapshots. Recorded playback reads only from here; the
    # Hub is used solely for the live view.
    nas_root: str = os.getenv("REOLINK_NAS_ROOT", "Z:\\reo")
    # Background mirror: pre-remuxes the most recent NAS segments to a local,
    # seekable cache so scrubbing the live edge is instant. Cheap (local stream
    # copy, no Hub involvement). Set REOLINK_MIRROR=0 to disable.
    mirror_enabled: bool = _bool(os.getenv("REOLINK_MIRROR"), True)


settings = Settings()
