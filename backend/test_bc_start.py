"""Test the CORRECT start_replay (cmd 5) decoded from the app capture:
FileInfoList/FileInfo with <Id>=full path, <uid>, <supportSub>, <playSpeed>,
<streamType>. Preceded by seek (cmd 123). Check the Hub accepts it (200)."""
from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, time as dtime

from reolink_aio.api import Host
from app.config import settings

CH = 0


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data()
    await host.get_states()
    await host.baichuan.get_ports()

    now = datetime.now()
    d0 = datetime.combine(now.date(), dtime(0, 0, 0))
    _s, files = await host.request_vod_files(CH, d0, now, stream="main")
    seg = ([f for f in files if f.duration.total_seconds() >= 240] or files)[-1]
    path = seg.file_name  # full /mnt/sda/...vref

    # uid: app sent "9527000KB5OX1LOE"; path has ".../U109527000KB5OX1LOE-Front Door/..."
    m = re.search(r"/U10([0-9A-Za-z]+)-", path)
    uid = m.group(1) if m else (host.baichuan.http_api.camera_uid(CH) or "").split("_")[0]
    print(f"path={path[-50:]!r}\nuid={uid!r}")

    st = seg.start_time.replace(tzinfo=None)  # seek time must match the file
    print(f"seek/file time: {st:%Y-%m-%d %H:%M:%S}")
    seq = int(time.time())
    seek = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
            f'<channelId>{CH}</channelId><seq>{seq}</seq>'
            f'<seekTime><year>{st.year}</year><month>{st.month}</month><day>{st.day}</day>'
            f'<hour>{st.hour}</hour><minute>{st.minute}</minute><second>{st.second}</second>'
            f'</seekTime></ReplaySeek></body>')
    try:
        await host.baichuan.send(cmd_id=123, channel=CH, body=seek)
        print("seek (123): OK")
    except Exception as exc:
        print(f"seek (123): {exc}")

    start = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
             f'<channelId>{CH}</channelId>'
             f'<Id>{path}</Id>'
             f'<uid>{uid}</uid>'
             f'<supportSub>0</supportSub>'
             f'<playSpeed>1</playSpeed>'
             f'<streamType>mainStream</streamType>'
             f'</FileInfo></FileInfoList></body>')
    try:
        r = await host.baichuan.send(cmd_id=5, channel=CH, body=start)
        print(f"\n*** start_replay (cmd 5): ACCEPTED! resp len={len(r)} ***")
        print(f"resp: {r[:300]!r}")
    except Exception as exc:
        print(f"\nstart_replay (cmd 5): {type(exc).__name__}: {exc}")

    await host.logout()


if __name__ == "__main__":
    asyncio.run(main())
