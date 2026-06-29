"""Decrypt the captured Baichuan login + replay commands.

1. Parse Baichuan messages (both directions) from a tshark field export.
2. Find the nonce (BC/XOR-encrypted login response) -> derive session AES key.
3. AES-decrypt the app's command bodies (esp. cmd 14 file-list and cmd 5 start).
"""
from __future__ import annotations

import subprocess
import sys

from Cryptodome.Cipher import AES
from reolink_aio.baichuan.util import AES_IV, XML_KEY, md5_str_modern, decrypt_baichuan
from app.config import settings

TSHARK = r"C:\Program Files\Wireshark\tshark.exe"
PCAP = sys.argv[1] if len(sys.argv) > 1 else r"C:\dev\reo\bc_login.pcapng"
HUB = settings.host
MAGIC = bytes.fromhex("f0debc0a")


def export(direction_dst: str) -> bytes:
    """Concatenate TCP payloads for one direction (by dst IP)."""
    out = subprocess.run(
        [TSHARK, "-r", PCAP, "-Y", f"ip.dst=={direction_dst} && tcp.len>0",
         "-T", "fields", "-e", "tcp.payload"],
        capture_output=True, text=True,
    ).stdout
    blob = bytearray()
    for line in out.splitlines():
        line = line.strip()
        if line:
            try:
                blob += bytes.fromhex(line)
            except ValueError:
                pass
    return bytes(blob)


def parse_messages(blob: bytes):
    """Yield (cmd_id, ch_id, status, mclass, payload_off, body_bytes)."""
    i = 0
    while True:
        idx = blob.find(MAGIC, i)
        if idx < 0 or idx + 20 > len(blob):
            break
        hdr = blob[idx:idx + 24]
        cmd_id = int.from_bytes(hdr[4:8], "little")
        body_len = int.from_bytes(hdr[8:12], "little")
        ch_id = hdr[12]
        status = int.from_bytes(hdr[16:18], "little")
        mclass = hdr[18:20].hex()
        if mclass in ("1465", "1466"):  # legacy / modern 20-byte header
            hlen, payload_off = 20, 0
        else:  # 1464 / 0000 modern 24-byte header
            hlen, payload_off = 24, int.from_bytes(hdr[20:24], "little")
        body = blob[idx + hlen: idx + hlen + body_len]
        yield cmd_id, ch_id, status, mclass, payload_off, body
        i = idx + 4


def aes_dec(data: bytes) -> bytes:
    return AES.new(AES_KEY, AES.MODE_CFB, iv=AES_IV, segment_size=128).decrypt(data)


# --- 1. find the app's IP (non-Hub side), then nonce from Hub->app (BC/XOR) ---
conv = subprocess.run([TSHARK, "-r", PCAP, "-T", "fields", "-e", "ip.src", "-e", "ip.dst",
                       "-Y", "tcp.len>0"], capture_output=True, text=True).stdout
ips = set()
for ln in conv.splitlines():
    for ip in ln.split("\t"):
        ip = ip.strip()
        if ip and ip != HUB:
            ips.add(ip)
app_ip = sorted(ips)[0] if ips else None
print(f"Hub={HUB} app={app_ip}")

from_hub = export(app_ip)   # dst == app  => from Hub
from_app = export(HUB)      # dst == Hub  => from app

nonce = None
for cmd_id, ch_id, status, mclass, poff, body in parse_messages(from_hub):
    # The nonce is in a small cmd 1 login response. Skip media/large bodies.
    if not body or len(body) > 1500 or cmd_id != 1:
        continue
    for off in range(256):
        try:
            txt = decrypt_baichuan(body, off)
        except Exception:
            continue
        if "nonce" in txt and "<" in txt:
            import re
            m = re.search(r"<nonce>(.*?)</nonce>", txt)
            if m:
                nonce = m.group(1)
                print(f"NONCE found (cmd {cmd_id}, xor offset {off}): {nonce}")
                break
    if nonce:
        break

if not nonce:
    print("nonce not found via BC; dumping Hub->app cmd_ids:")
    for cmd_id, ch_id, status, mclass, poff, body in parse_messages(from_hub):
        print(f"  cmd {cmd_id} class {mclass} len {len(body)} status {status}")
    sys.exit(1)

AES_KEY = md5_str_modern(f"{nonce}-{settings.password}")[0:16].encode("utf8")
print(f"AES key: {AES_KEY!r}\n")

# --- 2. decrypt app->Hub command bodies (focus cmd 14, 5, 15, 16, 123) ---
for cmd_id, ch_id, status, mclass, poff, body in parse_messages(from_app):
    if cmd_id not in (5, 8, 14, 15, 16, 123, 13):
        continue
    if not body:
        continue
    try:
        if poff > 0:
            ext = aes_dec(body[:poff]); bdy = aes_dec(body[poff:])
            text = ext.decode("utf8", "replace") + "\n---body---\n" + bdy.decode("utf8", "replace")
        else:
            text = aes_dec(body).decode("utf8", "replace")
    except Exception as exc:
        text = f"<decrypt error: {exc}>"
    print(f"===== cmd {cmd_id} (len {len(body)}, payload_off {poff}) =====")
    print(text.strip()[:1200])
    print()
