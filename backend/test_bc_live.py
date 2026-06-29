"""Milestone 2c proof: live Baichuan replay -> streaming demux -> ffmpeg
fragmented MP4 in realtime. Writes bc_live.mp4; then ffprobe it."""
from __future__ import annotations

import asyncio
from datetime import datetime

from reolink_aio.api import Host
from app.config import settings
from app.go2rtc import _find_ffmpeg as ff
import bc_replay
import bc_demux

OUT = "C:/dev/reo/bc_live.mp4"


async def main() -> None:
    host = Host(settings.host, settings.username, settings.password,
                port=settings.port, use_https=settings.use_https)
    await host.get_host_data(); await host.get_states(); await host.baichuan.get_ports()
    now = datetime.now()
    uid, files = await bc_replay.list_files(host, now, stream="mainStream")
    mains = [(fid, st) for fid, st in files if "F00_880" in fid]
    fid, st = mains[len(mains) // 2]  # a complete main file mid-day
    print(f"playing {fid[-50:]} @ {st['hour']:02d}:{st['minute']:02d}:{st['second']:02d}")

    key = host.baichuan._aes_key
    framer = bc_demux.BaichuanFramer()
    demux = bc_demux.BcMediaStreamDemuxer()

    ffmpeg = await asyncio.create_subprocess_exec(
        ff(), "-hide_banner", "-loglevel", "error",
        "-f", "hevc", "-i", "pipe:0",
        "-c:v", "copy",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4", OUT,
        stdin=asyncio.subprocess.PIPE,
    )
    counter = {"h265": 0}
    proto = host.baichuan._connection._protocol
    orig_dr = proto.data_received

    def tee(data: bytes):
        for cmd_id, mclass, poff, body in framer.feed(data):
            if cmd_id == 5 and mclass == "416a":
                media = bc_demux.media_from_message(body, poff, key)
                h265 = demux.feed(media)
                if h265:
                    counter["h265"] += len(h265)
                    try:
                        ffmpeg.stdin.write(h265)
                    except Exception:
                        pass
        orig_dr(data)

    proto.data_received = tee
    await bc_replay.start_replay(host, uid, fid, st, stream="mainStream")
    print("streaming 12s -> ffmpeg...")
    await asyncio.sleep(12)
    await bc_replay.stop_replay(host)
    proto.data_received = orig_dr
    try:
        ffmpeg.stdin.close()
    except Exception:
        pass
    await ffmpeg.wait()
    print(f"fed {counter['h265']/1024:.0f} KB H.265 to ffmpeg")
    await host.logout()


if __name__ == "__main__":
    asyncio.run(main())
