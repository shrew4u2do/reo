"""MILESTONE 1 (desktop variant): send CMD 0x17d binary replay over Baichuan.

The Hub rejected XML start_replay (MSG 5/8); it needs the desktop binary replay
(0x17d, 0x944-byte payload with the full recording path). reolink_aio's send()
is XML-only, so we build the Baichuan frame manually (reusing its AES + socket)
and capture the streamed media via the push_callback.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, time as dtime

from reolink_aio.api import Host
from reolink_aio.baichuan.util import HEADER_MAGIC
from app.config import settings

CH = 0
DUMP = "C:/dev/reo/bin/bc_desktop_dump.bin"
DESKTOP_LEN = 0x944


def build_desktop_payload(channel_id: int, path: str) -> bytes:
    out = bytearray(DESKTOP_LEN)
    out[0:8] = (2).to_bytes(8, "little")
    out[8:12] = (0x82F).to_bytes(4, "little")
    out[12:16] = (8).to_bytes(4, "little")
    out[16:20] = (500).to_bytes(4, "little")
    out[20:24] = (channel_id).to_bytes(4, "little")  # after 20-byte inner header
    # [24:56] = 32 zeros
    pstart = 20 + 4 + 32
    pb = path.encode("utf8")[:0x3FF]
    out[pstart:pstart + len(pb)] = pb
    return bytes(out)


def build_frame(bc, cmd_id: int, payload: bytes, ch_id: int, encrypt: bool):
    bc._mess_id = (bc._mess_id + 1) % 16777216
    mess_id = bc._mess_id
    body = bc._aes_encrypt(payload) if encrypt else payload
    mess_len = len(body)
    header = (
        bytes.fromhex(HEADER_MAGIC)
        + cmd_id.to_bytes(4, "little")
        + mess_len.to_bytes(4, "little")
        + bytes([ch_id]) + mess_id.to_bytes(3, "little")
        + bytes.fromhex("0000" + "1464")
        + (0).to_bytes(4, "little")  # payload_offset = body length = 0 (all payload)
    )
    full_mess_id = int.from_bytes(bytes([ch_id]) + mess_id.to_bytes(3, "little"), "little")
    return header + body, full_mess_id


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data()
    await host.get_states()
    await host.baichuan.get_ports()
    bc = host.baichuan

    now = datetime.now()
    d0 = datetime.combine(now.date(), dtime(0, 0, 0))
    _s, files = await host.request_vod_files(CH, d0, now, stream="main")
    seg = ([f for f in files if f.duration.total_seconds() >= 240] or files)[-1]
    path = seg.file_name  # full /mnt/sda/.../RecM04...vref path
    st = seg.start_time.replace(tzinfo=None)
    print(f"path={path[-55:]!r} start={st:%H:%M:%S}")

    proto = bc._connection._protocol
    orig_cb = proto._push_callback
    cap = {"bytes": 0, "packets": 0, "first": b"", "t0": None}
    fh = open(DUMP, "wb")

    def my_cb(cmd_id, data, len_header, payload):
        if cmd_id in (0x17D, 5, 8):
            if cap["t0"] is None:
                cap["t0"] = time.monotonic(); cap["first"] = bytes(data[:64])
            cap["bytes"] += len(payload); cap["packets"] += 1
            fh.write(payload); return
        try:
            orig_cb(cmd_id, data, len_header, payload)
        except Exception:
            pass

    proto._push_callback = my_cb

    # seek first (this worked before)
    seq = int(time.time())
    seek = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
            f'<channelId>{CH}</channelId><seq>{seq}</seq><seekTime>'
            f'<year>{st.year}</year><month>{st.month}</month><day>{st.day}</day>'
            f'<hour>{st.hour}</hour><minute>{st.minute}</minute><second>{st.second}</second>'
            f'</seekTime></ReplaySeek></body>')
    try:
        await bc.send(cmd_id=123, channel=CH, body=seek)
        print("seek OK")
    except Exception as exc:
        print(f"seek: {exc}")

    payload = build_desktop_payload(CH, path)
    accepted = False
    for encrypt in (False, True):
        for ch_id in (1, 0, CH):
            frame, fmid = build_frame(bc, 0x17D, payload, ch_id, encrypt)
            try:
                await bc._connect_if_needed()
                data, lh, pl = await bc._connection.send(frame, 0x17D, fmid, None, "")
                print(f"0x17d enc={encrypt} ch_id={ch_id}: ACCEPTED (resp payload {len(pl)} bytes)")
                accepted = True
                break
            except Exception as exc:
                print(f"0x17d enc={encrypt} ch_id={ch_id}: {type(exc).__name__}: {str(exc)[:90]}")
        if accepted:
            break

    await asyncio.sleep(12)
    fh.close()
    proto._push_callback = orig_cb
    dt = (time.monotonic() - cap["t0"]) if cap["t0"] else 0
    print(f"\n=== RESULT === accepted={accepted} packets={cap['packets']} "
          f"bytes={cap['bytes']} ({cap['bytes']/1024:.0f} KB) "
          f"{(cap['bytes']/1024/dt) if dt else 0:.0f} KB/s")
    print(f"first 64: {cap['first'].hex()}")
    await host.logout()


if __name__ == "__main__":
    asyncio.run(main())
