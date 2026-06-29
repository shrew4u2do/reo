"""Make-or-break connectivity test for the Reolink Home Hub.

Run:  python test_connection.py

Logs into the Hub, prints device info, lists every camera channel, and shows
the RTSP main/sub URLs we'll use for live view. If this works, everything else
is downstream of it.
"""
from __future__ import annotations

import asyncio
import sys

from reolink_aio.api import Host

from app.config import settings


async def main() -> int:
    if not settings.host or not settings.password:
        print("ERROR: REOLINK_HOST / REOLINK_PASSWORD not set. Copy "
              "backend/.env.example to backend/.env and fill it in.")
        return 1

    print(f"Connecting to {settings.host}:{settings.port} "
          f"(https={settings.use_https}) as {settings.username} ...")
    host = Host(
        settings.host,
        settings.username,
        settings.password,
        port=settings.port,
        use_https=settings.use_https,
    )

    try:
        await host.get_host_data()   # login + capabilities
        await host.get_states()      # current channel states
    except Exception as exc:  # noqa: BLE001 - surface anything to the user
        print(f"\nFAILED to connect: {type(exc).__name__}: {exc}")
        return 2

    print("\n=== CONNECTED ===")
    print(f"Model:     {host.model}")
    print(f"Firmware:  {host.sw_version}")
    print(f"Is NVR/Hub:{host.is_nvr}")
    print(f"Channels:  {host.num_channels}")

    print("\n=== CAMERAS ===")
    for ch in host.channels:
        name = host.camera_name(ch)
        online = host.camera_online(ch)
        try:
            main = await host.get_rtsp_stream_source(ch, "main")
            sub = await host.get_rtsp_stream_source(ch, "sub")
        except Exception:  # noqa: BLE001
            main = sub = "(unavailable)"
        print(f"\n[ch {ch}] {name}  online={online}")
        print(f"   main: {main}")
        print(f"   sub : {sub}")

    await host.logout()
    print("\nOK - Hub connectivity confirmed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
