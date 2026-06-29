"""Verify in-place re-seek: keep ONE replay open and reposition with cmd 123,
with the tee FILTERING media (416a) out of reolink_aio's parser so command
sends stay in sync. Prints media KB per 5s window after each re-seek."""
from __future__ import annotations

import asyncio
import time as _time
from datetime import datetime

from reolink_aio.api import Host
from app.config import settings
import app.bc_replay as bc_replay
import app.bc_demux as bc_demux

CH = 0


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data(); await host.get_states(); await host.baichuan.get_ports()
    now = datetime.now()
    uid, files = await bc_replay.list_files(host, CH, now, stream="mainStream")
    mains = [(fid, st) for fid, st in files if "F00_880" in fid]
    fid, st0 = mains[len(mains) // 2]
    print(f"file {fid[-36:]} start {st0['hour']:02d}:{st0['minute']:02d}:{st0['second']:02d}")

    key = host.baichuan._aes_key
    framer = bc_demux.BaichuanFramer()
    demux = bc_demux.BcMediaStreamDemuxer()
    counter = {"h265": 0}
    proto = host.baichuan._connection._protocol
    orig = proto.data_received

    def tee(data):
        keep = bytearray()
        for cmd_id, mclass, poff, body, raw in framer.feed(data):
            if cmd_id == 5 and mclass == "416a":
                counter["h265"] += len(demux.feed(bc_demux.media_from_message(body, poff, key)))
            else:
                keep += raw  # forward only non-media to reolink_aio (stays in sync)
        if keep:
            orig(bytes(keep))
    proto.data_received = tee

    async def send123(t):
        seq = int(_time.time())
        seek = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><ReplaySeek version="1.1">'
                f'<channelId>{CH}</channelId><seq>{seq}</seq><seekTime>'
                f'<year>{t["year"]}</year><month>{t["month"]}</month><day>{t["day"]}</day>'
                f'<hour>{t["hour"]}</hour><minute>{t["minute"]}</minute><second>{t["second"]}</second>'
                f'</seekTime></ReplaySeek></body>')
        try:
            await asyncio.wait_for(host.baichuan.send(cmd_id=123, body=seek), 3)
        except Exception as e:
            print(f"   (cmd123 send: {type(e).__name__})")

    def at(secs_into):
        base = st0["hour"] * 3600 + st0["minute"] * 60 + st0["second"] + secs_into
        return {"year": st0["year"], "month": st0["month"], "day": st0["day"],
                "hour": base // 3600, "minute": (base % 3600) // 60, "second": base % 60}

    async def window(label):
        counter["h265"] = 0
        await asyncio.sleep(5)
        print(f"{label}: {counter['h265']//1024} KB / 5s")

    # initial start (cmd123 seek + cmd5 start); cmd5 response is media (filtered),
    # so fire it without waiting.
    await send123(at(0))
    body5 = (f'<?xml version="1.0" encoding="UTF-8" ?>\n<body><FileInfoList version="1.1"><FileInfo>'
             f'<channelId>{CH}</channelId><Id>{fid}</Id><uid>{uid}</uid>'
             f'<supportSub>0</supportSub><playSpeed>1</playSpeed><streamType>mainStream</streamType>'
             f'</FileInfo></FileInfoList></body>')
    try:
        await asyncio.wait_for(host.baichuan.send(cmd_id=5, body=body5), 2)
    except Exception:
        pass
    await window("after start")

    # Cross 5-min file boundaries with cmd 123 ONLY (no cmd5). +330s and +700s
    # land in later files; if media flows, cmd123 handles cross-file seeking.
    for secs in (330, 700, 60):
        await send123(at(secs))
        print(f"  re-seek -> +{secs}s (cross-file)")
        await window(f"after reseek +{secs}")

    proto.data_received = orig
    await host.logout()


if __name__ == "__main__":
    asyncio.run(main())
