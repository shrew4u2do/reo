"""Drive the (reverse-engineered) Baichuan replay handshake via reolink_aio.

Flow: file-list handle (cmd 14) -> list (cmd 15) -> seek (cmd 123) -> start
(cmd 5). All sent WITHOUT the channel= param (no <Extension>; channelId goes in
the body, matching the desktop app). See MEMORY for the full protocol notes.
"""
from __future__ import annotations

import asyncio
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, time as dtime

from reolink_aio.api import Host

CMD_TIMEOUT = 8.0  # never let a Baichuan command wedge the playback lock


async def _send(host: Host, cmd_id: int, body: str, timeout: float = CMD_TIMEOUT) -> str:
    return await asyncio.wait_for(host.baichuan.send(cmd_id=cmd_id, body=body), timeout)

RECTYPE = ("manual, sched, io, md, people, face, vehicle, dog_cat, visitor, other, "
           "package, crossline, intrusion, loitering, legacy, loss")
FILEREC = "".join(f"<i>{t}</i>" for t in
                  ["manual", "sched", "io", "md", "people", "face", "vehicle", "dog_cat",
                   "visitor", "other", "package", "crossline", "intrusion", "loitering",
                   "legacy", "loss", "answer", "nonmotorveh"])


def camera_uid_from_path(path: str) -> str:
    m = re.search(r"/U10([0-9A-Za-z]+)-", path)
    return m.group(1) if m else ""


async def list_files(host: Host, channel: int, day: datetime, stream: str = "mainStream"):
    """Return (uid, [(Id, startTime_dict)]) for the WHOLE day.

    Built from request_vod_files (the standard search), which returns the
    complete list — the Baichuan cmd 14/15 file list is truncated (~40 files).
    Each VOD_file.file_name is the /mnt/sda/... path the replay uses as <Id>.
    """
    cgi_stream = "sub" if stream == "subStream" else "main"
    start = datetime.combine(day.date(), dtime(0, 0, 0))
    end = datetime.combine(day.date(), dtime(23, 59, 59))
    _s, files = await host.request_vod_files(channel, start, end, stream=cgi_stream)
    if not files:
        return "", []
    uid = camera_uid_from_path(files[-1].file_name)
    out = []
    for f in files:
        t = f.start_time
        d = {"year": t.year, "month": t.month, "day": t.day,
             "hour": t.hour, "minute": t.minute, "second": t.second}
        out.append((f.file_name, d))
    out.sort(key=lambda x: (x[1]["hour"], x[1]["minute"], x[1]["second"]))
    return uid, out


async def seek(host: Host, channel: int, start: dict) -> None:
    """Reposition an already-running replay (cmd 123 only). Best-effort: the
    reposition happens on write; the response may be slow amid media, so don't
    block on it."""
    seq = int(time.time())
    body = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
            f'<channelId>{channel}</channelId><seq>{seq}</seq><seekTime>'
            f'<year>{start["year"]}</year><month>{start["month"]}</month><day>{start["day"]}</day>'
            f'<hour>{start["hour"]}</hour><minute>{start["minute"]}</minute><second>{start["second"]}</second>'
            f'</seekTime></ReplaySeek></body>')
    try:
        await _send(host, 123, body, timeout=1.5)
    except Exception:
        pass


async def start_replay(host: Host, channel: int, uid: str, file_id: str, start: dict, stream: str = "mainStream") -> None:
    await seek(host, channel, start)
    support_sub = 1 if stream == "subStream" else 0
    body5 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
             f'<channelId>{channel}</channelId><Id>{file_id}</Id><uid>{uid}</uid>'
             f'<supportSub>{support_sub}</supportSub><playSpeed>1</playSpeed><streamType>{stream}</streamType>'
             f'</FileInfo></FileInfoList></body>')
    # cmd 5's "response" is the media stream itself (filtered from reolink_aio),
    # so the reply never arrives — just write it and move on quickly.
    try:
        await _send(host, 5, body5, timeout=0.6)
    except Exception:
        pass


async def stop_replay(host: Host) -> None:
    # Best-effort: tell the Hub to stop, but never block playback on it.
    try:
        await _send(host, 7, '<?xml version="1.0" encoding="UTF-8" ?>\n<body></body>', timeout=3.0)
    except Exception:
        pass
