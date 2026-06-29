"""Probe what the Hub returns for recordings, so we build Phase 3 against
real data. Searches today (local time) for channel 0, both sub and main.

Run:  python test_recordings.py
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, time

from reolink_aio.api import Host
from reolink_aio.enums import VodRequestType

from app.config import settings


async def main() -> int:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data()
    await host.get_states()

    ch = 0
    now = datetime.now()
    start = datetime.combine(now.date(), time(0, 0, 0))
    end = datetime.combine(now.date(), time(23, 59, 59))
    print(f"is_hub={host.is_hub}  is_nvr={host.is_nvr}")
    print(f"Searching ch{ch} {start} .. {end}\n")

    for stream in ("sub", "main"):
        print(f"===== stream={stream} =====")
        try:
            statuses, files = await host.request_vod_files(ch, start, end, stream=stream)
        except Exception as exc:  # noqa: BLE001
            print(f"  search FAILED: {type(exc).__name__}: {exc}\n")
            continue
        print(f"  days with recordings: "
              f"{[d.isoformat() for s in statuses for d in s]}")
        print(f"  segments: {len(files)}")
        for f in files[:6]:
            print(f"   - {f.start_time:%H:%M:%S} -> {f.end_time:%H:%M:%S} "
                  f"({f.duration}) type={f.type} size={f.size} name={f.file_name!r}")
        if files:
            f0 = files[0]
            # How we'd fetch a clip for this segment.
            for rt in (VodRequestType.DOWNLOAD, VodRequestType.NVR_DOWNLOAD):
                try:
                    mime, url = await host.get_vod_source(ch, f0.file_name, stream, rt)
                    # Redact credentials in the printed URL.
                    safe = url.split("&user=")[0].split("&token=")[0]
                    print(f"  {rt.value}: mime={mime}  url={safe}…")
                except Exception as exc:  # noqa: BLE001
                    print(f"  {rt.value}: FAILED {type(exc).__name__}: {exc}")
        print()

    await host.logout()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
