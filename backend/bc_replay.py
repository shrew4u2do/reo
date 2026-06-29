"""Baichuan replay helper: drive the cracked replay handshake via reolink_aio
and expose the media stream.

Milestone 2a: capture the raw Hub->app bytes during a replay to a file so we can
analyze the cmd5/class-416a media framing.
"""
from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime

from reolink_aio.api import Host

CH = 0
RECTYPE = "manual, sched, io, md, people, face, vehicle, dog_cat, visitor, other, package, crossline, intrusion, loitering, legacy, loss"
FILEREC = "".join(f"<i>{t}</i>" for t in
                  ["manual", "sched", "io", "md", "people", "face", "vehicle", "dog_cat", "visitor",
                   "other", "package", "crossline", "intrusion", "loitering", "legacy", "loss", "answer", "nonmotorveh"])


def camera_uid_from_path(path: str) -> str:
    m = re.search(r"/U10([0-9A-Za-z]+)-", path)
    return m.group(1) if m else ""


async def list_files(host: Host, day: datetime, stream: str = "mainStream") -> list[tuple[str, dict]]:
    """Return [(Id, startTime-dict)] for the day via the Baichuan file-list."""
    _s, files = await host.request_vod_files(CH, datetime.combine(day.date(), dtime(0, 0, 0)), day, stream="main")
    uid = camera_uid_from_path(files[-1].file_name)
    body14 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
              f'<uid>{uid}</uid><searchAITrack>1</searchAITrack><channelId>{CH}</channelId>'
              f'<logicChnBitmap>255</logicChnBitmap><streamType>{stream}</streamType>'
              f'<recordType>{RECTYPE}</recordType><fileRecordType>{FILEREC}</fileRecordType>'
              f'<startTime><year>{day.year}</year><month>{day.month}</month><day>{day.day}</day><hour>0</hour><minute>0</minute><second>0</second></startTime>'
              f'<endTime><year>{day.year}</year><month>{day.month}</month><day>{day.day}</day><hour>23</hour><minute>59</minute><second>59</second></endTime>'
              f'</FileInfo></FileInfoList></body>')
    r = await host.baichuan.send(cmd_id=14, body=body14)
    handle = ET.fromstring(r).findtext(".//handle")
    out: list[tuple[str, dict]] = []
    body15 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
              f'<channelId>{CH}</channelId><uid>{uid}</uid><searchAITrack>1</searchAITrack>'
              f'<handle>{handle}</handle></FileInfo></FileInfoList></body>')
    r = await host.baichuan.send(cmd_id=15, body=body15)
    for fi in ET.fromstring(r).findall(".//FileInfo"):
        fid = fi.findtext("Id") or fi.findtext("name")
        stt = fi.find("startTime")
        if fid and stt is not None:
            d = {t: int(stt.findtext(t) or 0) for t in ("year", "month", "day", "hour", "minute", "second")}
            out.append((fid, d))
    return uid, out


async def start_replay(host: Host, uid: str, file_id: str, start: dict, stream: str = "mainStream") -> None:
    seq = int(time.time())
    seek = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
            f'<channelId>{CH}</channelId><seq>{seq}</seq><seekTime>'
            f'<year>{start["year"]}</year><month>{start["month"]}</month><day>{start["day"]}</day>'
            f'<hour>{start["hour"]}</hour><minute>{start["minute"]}</minute><second>{start["second"]}</second>'
            f'</seekTime></ReplaySeek></body>')
    await host.baichuan.send(cmd_id=123, body=seek)
    support_sub = 1 if stream == "subStream" else 0
    body5 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
             f'<channelId>{CH}</channelId><Id>{file_id}</Id><uid>{uid}</uid>'
             f'<supportSub>{support_sub}</supportSub><playSpeed>1</playSpeed><streamType>{stream}</streamType>'
             f'</FileInfo></FileInfoList></body>')
    await host.baichuan.send(cmd_id=5, body=body5)


async def stop_replay(host: Host) -> None:
    try:
        await host.baichuan.send(cmd_id=7, body='<?xml version="1.0" encoding="UTF-8" ?>\n<body></body>')
    except Exception:
        pass
