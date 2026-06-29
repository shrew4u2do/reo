"""Milestone 2a: capture the raw Baichuan replay stream in-process and analyze
the cmd5/class-416a media framing + payload format (H.265? BcMedia? MP4?)."""
from __future__ import annotations

import asyncio
from datetime import datetime

from reolink_aio.api import Host
from app.config import settings
import bc_replay

RAW = "C:/dev/reo/bc_media_raw.bin"
MAGIC = bytes.fromhex("f0debc0a")


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data(); await host.get_states(); await host.baichuan.get_ports()
    now = datetime.now()
    uid, files = await bc_replay.list_files(host, now, stream="mainStream")
    # choose a complete main (4K) file
    fid, st = None, None
    for f_id, s in files:
        if "F00_880" in f_id or "RecM" in f_id:
            fid, st = f_id, s
    if not fid:
        fid, st = files[-1]
    print(f"uid={uid} file={fid[-55:]} start={st}")

    # Tap raw Hub->app bytes
    proto = host.baichuan._connection._protocol
    orig_dr = proto.data_received
    fh = open(RAW, "wb")
    counter = {"bytes": 0}

    def tee(data: bytes):
        fh.write(data); counter["bytes"] += len(data)
        orig_dr(data)

    proto.data_received = tee

    t0 = asyncio.get_event_loop().time()
    await bc_replay.start_replay(host, uid, fid, st, stream="mainStream")
    print("start_replay accepted; capturing 12s...")
    await asyncio.sleep(12)
    await bc_replay.stop_replay(host)
    dt = asyncio.get_event_loop().time() - t0
    proto.data_received = orig_dr
    fh.close()
    aes_key = host.baichuan._aes_key  # grab session key for decrypt
    print(f"captured {counter['bytes']/1024:.0f} KB in {dt:.1f}s = {counter['bytes']/1024/dt:.0f} KB/s")
    print(f"aes_key={aes_key!r}")
    await host.logout()

    import bc_demux
    blob = open(RAW, "rb").read()
    media = bc_demux.reassemble_media(blob, aes_key)
    open("C:/dev/reo/bc_media.bin", "wb").write(media)  # reassembled, for offline demux iteration
    h265, stats = bc_demux.demux_h265(media)
    print(f"\nreassembled BcMedia: {len(media)} bytes")
    print(f"demux stats: {stats}")
    print(f"H.265 output: {len(h265)} bytes, start-codes={h265.count(bytes.fromhex('00000001'))}")
    print(f"H.265 head: {h265[:32].hex()}")
    open("C:/dev/reo/bc.h265", "wb").write(h265)
    print("wrote C:/dev/reo/bc.h265")


if __name__ == "__main__":
    asyncio.run(main())
