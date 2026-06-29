"""Full app-replica replay handshake (decrypted from capture):
cmd 14 (file-list handle) -> cmd 15 (list, get real Ids) -> seek (123) ->
cmd 5 (start, Id-based). reolink_aio.send() auto-decrypts responses.
"""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime

from reolink_aio.api import Host
from app.config import settings

CH = 0
RECTYPE = "manual, sched, io, md, people, face, vehicle, dog_cat, visitor, other, package, crossline, intrusion, loitering, legacy, loss"
FILEREC = "".join(f"<i>{t}</i>" for t in
                  ["manual","sched","io","md","people","face","vehicle","dog_cat","visitor",
                   "other","package","crossline","intrusion","loitering","legacy","loss","answer","nonmotorveh"])


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data(); await host.get_states(); await host.baichuan.get_ports()
    now = datetime.now()

    # derive uid from a recording path
    _s, files = await host.request_vod_files(CH, datetime.combine(now.date(), dtime(0,0,0)), now, stream="main")
    m = re.search(r"/U10([0-9A-Za-z]+)-", files[-1].file_name)
    uid = m.group(1)
    print(f"uid={uid}")

    # --- cmd 14: file-list handle (exact app XML) ---
    body14 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
              f'<uid>{uid}</uid><searchAITrack>1</searchAITrack><channelId>{CH}</channelId>'
              f'<logicChnBitmap>255</logicChnBitmap><streamType>mainStream</streamType>'
              f'<recordType>{RECTYPE}</recordType><fileRecordType>{FILEREC}</fileRecordType>'
              f'<startTime><year>{now.year}</year><month>{now.month}</month><day>{now.day}</day><hour>0</hour><minute>0</minute><second>0</second></startTime>'
              f'<endTime><year>{now.year}</year><month>{now.month}</month><day>{now.day}</day><hour>23</hour><minute>59</minute><second>59</second></endTime>'
              f'</FileInfo></FileInfoList></body>')
    r = await host.baichuan.send(cmd_id=14, body=body14)
    handle = ET.fromstring(r).findtext(".//handle")
    print(f"cmd 14 OK, handle={handle}")

    # --- cmd 15: list files by handle ---
    body15 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
              f'<channelId>{CH}</channelId><uid>{uid}</uid><searchAITrack>1</searchAITrack>'
              f'<handle>{handle}</handle></FileInfo></FileInfoList></body>')
    r = await host.baichuan.send(cmd_id=15, body=body15)
    root = ET.fromstring(r)
    fileinfos = root.findall(".//FileInfo")
    print(f"cmd 15 OK, {len(fileinfos)} files")
    # pick a complete file with an Id/name + startTime
    chosen = None
    for fi in fileinfos:
        fid = fi.findtext("Id") or fi.findtext("name")
        stt = fi.find("startTime")
        if fid and stt is not None and "RecM" in fid:
            chosen = (fid, stt)
    if not chosen:
        # fall back to first with any id
        for fi in fileinfos:
            fid = fi.findtext("Id") or fi.findtext("name")
            if fid:
                chosen = (fid, fi.find("startTime")); break
    if not chosen:
        print("no file Id in cmd15 response; dumping first 600 chars:"); print(r[:600]); await host.logout(); return
    fid, stt = chosen
    def gv(t, d=0):
        v = stt.findtext(t) if stt is not None else None
        return int(v) if v else d
    y,mo,d,h,mi,s = gv("year",now.year),gv("month",now.month),gv("day",now.day),gv("hour"),gv("minute"),gv("second")
    print(f"chosen Id={fid[-60:]!r} time={y}-{mo:02d}-{d:02d} {h:02d}:{mi:02d}:{s:02d}")

    # --- cmd 123 seek ---
    seq = int(time.time())
    seek = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
            f'<channelId>{CH}</channelId><seq>{seq}</seq>'
            f'<seekTime><year>{y}</year><month>{mo}</month><day>{d}</day><hour>{h}</hour><minute>{mi}</minute><second>{s}</second></seekTime>'
            f'</ReplaySeek></body>')
    await host.baichuan.send(cmd_id=123, body=seek)
    print("seek OK")

    # --- cmd 5 start ---
    start = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
             f'<channelId>{CH}</channelId><Id>{fid}</Id><uid>{uid}</uid>'
             f'<supportSub>0</supportSub><playSpeed>1</playSpeed><streamType>mainStream</streamType>'
             f'</FileInfo></FileInfoList></body>')
    try:
        r = await host.baichuan.send(cmd_id=5, body=start)
        print(f"\n*** start_replay (cmd 5): ACCEPTED! resp len={len(r)} ***")
        print("streaming for 18s (capture measures rate)...")
        await asyncio.sleep(18)
    except Exception as exc:
        print(f"\nstart_replay (cmd 5): {type(exc).__name__}: {exc}")
    try:
        await host.baichuan.send(cmd_id=7, body=start)  # stop
    except Exception:
        pass


if __name__ == "__main__":
    asyncio.run(main())
