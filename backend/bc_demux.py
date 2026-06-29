"""Demux a captured Baichuan replay stream into H.265 Annex-B.

Pipeline: raw Hub bytes -> extract cmd5/class-416a messages -> per-message E1
envelope (AES-decrypt the first <encryptLen> bytes of the media, rest is
cleartext) -> reassemble continuous BcMedia stream -> parse BcMedia frames
(IFRAME/PFRAME) -> concatenate raw H.265 (already Annex-B).
"""
from __future__ import annotations

import re
from Cryptodome.Cipher import AES
from reolink_aio.baichuan.util import AES_IV

MAGIC = bytes.fromhex("f0debc0a")
IFRAME_LO, IFRAME_HI = 0x63643030, 0x63643039
PFRAME_LO, PFRAME_HI = 0x63643130, 0x63643139
INFO_V1, INFO_V2 = 0x31303031, 0x32303031
AAC, ADPCM = 0x62773530, 0x62773130
PAD = 8


def _aes(key: bytes, b: bytes) -> bytes:
    return AES.new(key, AES.MODE_CFB, iv=AES_IV, segment_size=128).decrypt(b)


def reassemble_media(blob: bytes, key: bytes) -> bytes:
    """Extract + E1-decrypt cmd5/416a media into one continuous BcMedia stream."""
    out = bytearray()
    i = 0
    while True:
        idx = blob.find(MAGIC, i)
        if idx < 0 or idx + 24 > len(blob):
            break
        hdr = blob[idx:idx + 24]
        cmd_id = int.from_bytes(hdr[4:8], "little")
        blen = int.from_bytes(hdr[8:12], "little")
        mclass = hdr[18:20].hex()
        poff = int.from_bytes(hdr[20:24], "little")
        if cmd_id == 5 and mclass == "416a" and idx + 24 + blen <= len(blob):
            body = blob[idx + 24: idx + 24 + blen]
            ext = _aes(key, body[:poff]) if poff else b""
            m = re.search(rb"<encryptLen>(\d+)</encryptLen>", ext)
            enc_len = int(m.group(1)) if m else 0
            media = body[poff:]
            if enc_len > 0:
                media = _aes(key, media[:enc_len]) + media[enc_len:]
            out += media
        i = idx + 4
    return bytes(out)


class BaichuanFramer:
    """Incremental Baichuan TCP framer. feed(raw) -> list of complete messages
    as (cmd_id, mclass, payload_off, body_bytes)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, str, int, bytes]]:
        self.buf += data
        out = []
        while True:
            if len(self.buf) < 4:
                break
            if self.buf[:4] != MAGIC:
                idx = self.buf.find(MAGIC, 1)
                if idx < 0:
                    del self.buf[:-3]  # keep possible partial magic
                    break
                del self.buf[:idx]
                continue
            if len(self.buf) < 24:
                break
            cmd_id = int.from_bytes(self.buf[4:8], "little")
            blen = int.from_bytes(self.buf[8:12], "little")
            mclass = self.buf[18:20].hex()
            hlen = 20 if mclass in ("1465", "1466") else 24
            poff = int.from_bytes(self.buf[20:24], "little") if hlen == 24 else 0
            total = hlen + blen
            if len(self.buf) < total:
                break
            body = bytes(self.buf[hlen:total])
            out.append((cmd_id, mclass, poff, body))
            del self.buf[:total]
        return out


class BcMediaStreamDemuxer:
    """Incremental BcMedia -> H.26x Annex-B. feed(media) -> H.26x bytes ready so far."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.vtype = None

    def feed(self, media: bytes) -> bytes:
        self.buf += media
        out = bytearray()
        while True:
            chunk = self._one()
            if chunk is None:
                break
            out += chunk
        return bytes(out)

    def _one(self):
        b = self.buf
        n = len(b)
        if n < 4:
            return None
        magic = int.from_bytes(b[:4], "little")
        if IFRAME_LO <= magic <= IFRAME_HI or PFRAME_LO <= magic <= PFRAME_HI:
            if n < 24:
                return None
            psize = int.from_bytes(b[8:12], "little")
            addl = int.from_bytes(b[12:16], "little")
            total = 24 + addl + psize
            pad = (PAD - psize % PAD) % PAD
            total += pad
            if n < total:
                return None
            self.vtype = b[4:8].decode("ascii", "replace")
            data = bytes(b[24 + addl:24 + addl + psize])
            del self.buf[:total]
            return data
        if magic in (INFO_V1, INFO_V2):
            if n < 36:
                return None
            del self.buf[:36]
            return b""
        if magic == AAC:
            if n < 8:
                return None
            psize = int.from_bytes(b[4:6], "little")
            total = 8 + psize + (PAD - psize % PAD) % PAD
            if n < total:
                return None
            del self.buf[:total]
            return b""
        if magic == ADPCM:
            if n < 12:
                return None
            psize = int.from_bytes(b[4:6], "little")
            total = 4 + 8 + (psize - 4) + (PAD - psize % PAD) % PAD
            if n < total:
                return None
            del self.buf[:total]
            return b""
        # resync
        del self.buf[:1]
        return b""


def media_from_message(body: bytes, poff: int, key: bytes) -> bytes:
    """E1-decode one cmd5/416a message body -> media bytes (partial-AES handled)."""
    ext = _aes(key, body[:poff]) if poff else b""
    m = re.search(rb"<encryptLen>(\d+)</encryptLen>", ext)
    enc_len = int(m.group(1)) if m else 0
    media = body[poff:]
    if enc_len > 0:
        media = _aes(key, media[:enc_len]) + media[enc_len:]
    return media


def demux_h265(media: bytes) -> tuple[bytes, dict]:
    """Parse BcMedia frames -> concatenated H.26x Annex-B. Returns (data, stats)."""
    out = bytearray()
    p, n = 0, len(media)
    stats = {"iframe": 0, "pframe": 0, "info": 0, "aac": 0, "unknown": 0, "vtype": None}
    while p + 4 <= n:
        magic = int.from_bytes(media[p:p + 4], "little")
        if IFRAME_LO <= magic <= IFRAME_HI or PFRAME_LO <= magic <= PFRAME_HI:
            if p + 24 > n:
                break
            vtype = media[p + 4:p + 8]
            payload_size = int.from_bytes(media[p + 8:p + 12], "little")
            addl = int.from_bytes(media[p + 12:p + 16], "little")
            data_start = p + 24 + addl
            data_end = data_start + payload_size
            if data_end > n:
                break  # frame spans beyond buffer (need more data)
            out += media[data_start:data_end]
            stats["iframe" if magic <= IFRAME_HI else "pframe"] += 1
            stats["vtype"] = vtype.decode("ascii", "replace")
            pad = (PAD - payload_size % PAD) % PAD
            p = data_end + pad
        elif magic in (INFO_V1, INFO_V2):
            stats["info"] += 1
            p += 4 + 32  # magic + 32-byte info header
        elif magic == AAC:
            if p + 8 > n:
                break
            psize = int.from_bytes(media[p + 4:p + 6], "little")  # u16!
            pad = (PAD - psize % PAD) % PAD
            stats["aac"] += 1
            p += 8 + psize + pad  # magic(4)+psize(2)+psize_b(2)+data+pad
        elif magic == ADPCM:
            if p + 12 > n:
                break
            psize = int.from_bytes(media[p + 4:p + 6], "little")  # u16!
            pad = (PAD - psize % PAD) % PAD
            stats["aac"] += 1
            p += 4 + 8 + (psize - 4) + pad  # magic + 4 u16 fields + (psize-4) data + pad
        else:
            stats["unknown"] += 1
            p += 1  # resync byte-by-byte
    return bytes(out), stats
