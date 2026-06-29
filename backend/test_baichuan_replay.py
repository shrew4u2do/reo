"""MILESTONE 1: Baichuan replay over the Hub.

Flow: file-list handle (MSG 14) -> list files (MSG 15, real names) ->
ReplaySeek (MSG 123) -> start_replay (MSG 5/8). Capture cmd_id 5 media via the
connection push_callback.
"""
from __future__ import annotations

import asyncio
import time
import xml.etree.ElementTree as ET
from datetime import datetime

from reolink_aio.api import Host
from app.config import settings

CH = 0
DUMP = "C:/dev/reo/bin/bc_replay_dump.bin"

LIST_HANDLE_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<channelId>{ch}</channelId>
<recordType>{rt}</recordType>
<supportSub>0</supportSub>
<streamType>{stype}</streamType>
<startTime><year>{y}</year><month>{mo}</month><day>{d}</day><hour>0</hour><minute>0</minute><second>0</second></startTime>
<endTime><year>{y}</year><month>{mo}</month><day>{d}</day><hour>23</hour><minute>59</minute><second>59</second></endTime>
</FileInfo>
</FileInfoList>
</body>
"""

LIST_PAGE_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<channelId>{ch}</channelId>
<handle>{handle}</handle>
</FileInfo>
</FileInfoList>
</body>
"""

START_XML = """<?xml version="1.0" encoding="UTF-8" ?>
<body>
<FileInfoList version="1.1">
<FileInfo>
<uid>0</uid>
<name>{name}</name>
<channelId>{ch}</channelId>
<supportSub>0</supportSub>
<streamType>{stype}</streamType>
<startTime><year>{y}</year><month>{mo}</month><day>{d}</day><hour>{h}</hour><minute>{mi}</minute><second>{s}</second></startTime>
</FileInfo>
</FileInfoList>
</body>
"""


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data()
    await host.get_states()
    await host.baichuan.get_ports()

    now = datetime.now()
    stype = "mainStream"

    # Use reolink_aio's WORKING Baichuan VOD search (cmd 272/273) to get real names.
    from datetime import time as dtime
    d0 = datetime.combine(now.date(), dtime(0, 0, 0))
    _types, vod_dict = await host.baichuan.search_vod_type(CH, d0, now, stream="main")
    allf = [f for lst in vod_dict.values() for f in lst]
    allf.sort(key=lambda f: f.start_time)
    print(f"search_vod_type returned {len(allf)} files")
    for f in allf[-4:]:
        print(f"   name={f.file_name!r} start={f.start_time:%H:%M:%S}")
    if not allf:
        print("no files; aborting"); await host.logout(); return
    vf = allf[-1]
    name = vf.file_name  # bare 14-digit timestamp YYYYMMDDHHMMSS
    # Parse the time FROM the name (the block start), not the event time.
    y, mo, d = int(name[0:4]), int(name[4:6]), int(name[6:8])
    h, mi, s = int(name[8:10]), int(name[10:12]), int(name[12:14])
    print(f"\nusing name={name!r} start={y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")

    # --- hook push callback for media (cmd_id 5/8) ---
    proto = host.baichuan._connection._protocol
    orig_cb = proto._push_callback
    cap = {"bytes": 0, "packets": 0, "first": b"", "t0": None}
    fh = open(DUMP, "wb")

    def my_cb(cmd_id, data, len_header, payload):
        if cmd_id in (5, 8):
            if cap["t0"] is None:
                cap["t0"] = time.monotonic()
                cap["first"] = bytes(data[:64])
            cap["bytes"] += len(payload)
            cap["packets"] += 1
            fh.write(payload)
            return
        try:
            orig_cb(cmd_id, data, len_header, payload)
        except Exception:
            pass

    proto._push_callback = my_cb

    # --- MSG 123 seek then MSG 5/8 start ---
    seq = int(time.time())
    seek = f"""<?xml version="1.0" encoding="UTF-8" ?>
<body><ReplaySeek version="1.1"><channelId>{CH}</channelId><seq>{seq}</seq>
<seekTime><year>{y}</year><month>{mo}</month><day>{d}</day><hour>{h}</hour><minute>{mi}</minute><second>{s}</second></seekTime>
</ReplaySeek></body>"""
    try:
        await host.baichuan.send(cmd_id=123, channel=CH, body=seek)
        print("seek (123) OK")
    except Exception as exc:
        print(f"seek FAILED: {exc}")

    start = START_XML.format(name=name, ch=CH, stype=stype, y=y, mo=mo, d=d, h=h, mi=mi, s=s)
    for msg_id in (5, 8):
        try:
            r = await host.baichuan.send(cmd_id=msg_id, channel=CH, body=start)
            print(f"start_replay MSG {msg_id}: ACK len={len(r)}")
            break
        except Exception as exc:
            print(f"start_replay MSG {msg_id}: {type(exc).__name__}: {exc}")

    await asyncio.sleep(12)
    try:
        await host.baichuan.send(cmd_id=7, channel=CH, body=start)
    except Exception:
        pass
    fh.close()
    proto._push_callback = orig_cb

    dt = (time.monotonic() - cap["t0"]) if cap["t0"] else 0
    print(f"\n=== RESULT === packets={cap['packets']} bytes={cap['bytes']} "
          f"({cap['bytes']/1024:.0f} KB) in {dt:.1f}s = {(cap['bytes']/1024/dt) if dt else 0:.0f} KB/s")
    print(f"first 64 bytes: {cap['first'].hex()}")
    await host.logout()


if __name__ == "__main__":
    asyncio.run(main())
