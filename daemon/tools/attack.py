"""鍵を持たない第三者の BLE central として、デバイスが不正なフレームをすべて拒否するかを確かめる。

buddyd を止めてから実行する（NUS の接続は 1 本だけ）。デバイス側の拒否理由は USB シリアルのログで確認する。

    uv run python tools/attack.py record   # 正規ホストとして perm を 1 通送り、その行を保存する（鍵が必要）
    uv run python tools/attack.py attack   # 鍵なしで、平文・改ざん・別鍵・保存した行のリプレイを送る
"""

import asyncio
import base64
import json
import os
import pathlib
import sys

from bleak import BleakClient, BleakScanner

from cardbuddy import crypto as c
from cardbuddy.ble_link import NUS_RX, NUS_TX, BleConn

HOME = pathlib.Path(os.environ.get("CARDBUDDY_HOME", "~/.cardbuddy")).expanduser()
SAVED = pathlib.Path(__file__).with_name("recorded_line.txt")


async def connect():
    name = (HOME / "device").read_text().strip()
    dev = await BleakScanner.find_device_by_filter(lambda d, ad: (ad.local_name or d.name) == name, timeout=15)
    if dev is None:
        sys.exit(f"{name} not found (is buddyd stopped?)")
    client = BleakClient(dev)
    await client.connect()
    conn = BleConn()
    conn.client = client
    await client.start_notify(NUS_TX, conn.on_notify)
    return client, conn


async def hello(conn) -> bytes:
    nh = os.urandom(16)
    await conn.send_line(c.hello(c.ROLE_HOST, nh))
    nd = c.parse_hello(await asyncio.wait_for(conn.recv_line(), 5), c.ROLE_DEVICE)
    return nh, nd


async def record():
    key = (HOME / "key").read_bytes()
    client, conn = await connect()
    nh, nd = await hello(conn)
    sess = c.Session(*c.hkdf(key, nh, nd), c.DIR_H2D)
    line = sess.seal({"t": "perm", "n": 1, "req": "rattack", "id": "00000000", "name": "attack-test",
                      "tool": "Bash", "desc": "recorded by attack.py", "hint": "echo recorded", "full": True})
    await conn.send_line(line)
    SAVED.write_bytes(line)
    print("sent and saved a legit perm frame; the device should show it now")
    await asyncio.sleep(3)
    await client.disconnect()


def tamper(line: bytes, idx: int) -> bytes:
    raw = bytearray(base64.b64decode(line.strip()))
    raw[idx] ^= 1
    return base64.b64encode(bytes(raw)) + b"\n"


async def attack():
    client, conn = await connect()
    await hello(conn)
    saved = SAVED.read_bytes() if SAVED.exists() else None
    wrong = c.Session(*c.hkdf(os.urandom(32), b"\0" * 16, b"\0" * 16), c.DIR_H2D)
    cases = [
        ("plaintext perm_reply-looking json", json.dumps({"t": "perm", "n": 1, "req": "rplain", "tool": "Bash",
                                                          "hint": "x", "full": True}).encode() + b"\n"),
        ("frame sealed with a wrong key", wrong.seal({"t": "perm", "n": 1, "req": "rwrong", "tool": "Bash", "hint": "x", "full": True})),
    ]
    if saved:
        cases += [
            ("replay of a frame from a previous session", saved),
            ("replay with flipped ciphertext bit", tamper(saved, 8)),
            ("replay with flipped tag bit", tamper(saved, -1)),
        ]
    for why, line in cases:
        print("send:", why)
        await conn.send_line(line)
        await asyncio.sleep(1)
    try:
        got = await asyncio.wait_for(conn.recv_line(), 2)
        print("device replied (unexpected):", got[:60])
    except asyncio.TimeoutError:
        print("device sent nothing back (expected); check the serial log for the drop reasons")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run({"record": record, "attack": attack}[sys.argv[1]]())
