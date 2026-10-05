import asyncio
import base64
import functools
import logging
import os

import pytest

from cardbuddy import ble_link, crypto
from cardbuddy.ble_link import Link, LineBuffer
from cardbuddy.buddyd import Hub

KEY = bytes(range(32))
SID = "0f3c9a1e-aaaa-bbbb-cccc-000000000001"


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


class FakeConn:
    def __init__(self):
        self.to_dev = asyncio.Queue()
        self.to_host = asyncio.Queue()

    async def send_line(self, line):
        await self.to_dev.put(line)

    async def recv_line(self):
        line = await self.to_host.get()
        if line is None:
            raise ConnectionError("disconnected")
        return line


class Device:
    """デバイス側の模擬。crypto.Session(DIR_D2H) で送受信する。"""

    def __init__(self, conn, key=KEY):
        self.conn, self.key = conn, key

    async def handshake(self):
        self.nh = crypto.parse_hello(await self.conn.to_dev.get(), crypto.ROLE_HOST)
        nd = os.urandom(16)
        await self.conn.to_host.put(crypto.hello(crypto.ROLE_DEVICE, nd))
        self.s = crypto.Session(*crypto.hkdf(self.key, self.nh, nd), crypto.DIR_D2H)

    async def recv(self):
        return self.s.open(await asyncio.wait_for(self.conn.to_dev.get(), 2))

    async def send_line(self, line):
        await self.conn.to_host.put(line)


async def start(hub=None):
    hub = hub or Hub()
    link = Link(KEY, hub)
    hub.link = link
    conn = FakeConn()
    dev = Device(conn)
    task = asyncio.create_task(link.serve(conn))
    await dev.handshake()
    return hub, link, conn, dev, task


@aio
async def test_handshake_resends_sessions_and_pending():
    hub = Hub()
    hub.link = Link(KEY, hub)
    hub.table.upsert(SID, "/w/proj", "t", "perm", 0)
    req, _ = hub.table.add_perm(SID, "Bash", {"command": "ls"}, 0)
    hub, link, conn, dev, task = await start(hub)
    assert await dev.recv() == hub.table.sessions_msg()
    assert await dev.recv() == {"t": "perm", "n": 1, "req": req, "id": SID[:8], "name": "proj",
                                "tool": "Bash", "desc": "", "hint": "ls", "full": True}
    assert link.connected and dev.s.rx_ctr == 2
    await conn.to_host.put(None)
    assert await asyncio.wait_for(task, 2) is True
    assert not link.connected
    link.send({"t": "sessions", "s": []})


@aio
async def test_device_reply_reaches_hub_and_is_resolved():
    hub = Hub()
    hub.table.upsert(SID, "/w", "", "perm", 0)
    req, _ = hub.table.add_perm(SID, "Bash", {"command": "ls"}, 0)
    hub, link, conn, dev, task = await start(hub)
    await dev.recv(), await dev.recv()
    await dev.send_line(dev.s.seal({"t": "perm_reply", "req": req, "decision": "deny"}))
    assert await dev.recv() == {"t": "resolved", "req": req, "by": "device"}
    assert hub.table.perm_result(req) == {"decision": "deny"} and hub.table.pop_events(SID) == []
    task.cancel()


def _flip(line: bytes, idx: int) -> bytes:
    raw = bytearray(base64.b64decode(line))
    raw[idx] ^= 1
    return base64.b64encode(bytes(raw)) + b"\n"


@aio
async def test_rejects_tampered_replayed_and_plaintext_frames(caplog):
    caplog.set_level(logging.WARNING, logger="cardbuddy.ble_link")
    hub = Hub()
    hub.table.upsert(SID, "/w", "", "idle", 0)
    hub, link, conn, dev, task = await start(hub)
    await dev.recv()
    good = dev.s.seal({"t": "prompt", "n": 1, "id": SID[:8], "text": "first"})
    await dev.send_line(good)
    nxt = dev.s.seal({"t": "prompt", "n": 1, "id": SID[:8], "text": "second"})
    other = crypto.Session(*crypto.hkdf(KEY, dev.nh, os.urandom(16)), crypto.DIR_D2H)
    h2d = crypto.Session(dev.s.enc_key, dev.s.mac_key, crypto.DIR_H2D)
    h2d.tx_ctr = 5
    plaintext = b'{"t":"prompt","n":1,"id":"0f3c9a1e","text":"plain"}\n'
    cases = [
        ("replay", good),
        ("bad tag", _flip(nxt, 10)),  # ciphertext
        ("bad tag", _flip(nxt, -1)),  # tag
        ("bad tag", _flip(nxt, 6)),  # ctr
        ("bad tag", other.seal({"t": "prompt", "n": 1, "id": SID[:8], "text": "old"})),
        ("wrong direction", h2d.seal({"t": "prompt", "n": 1, "id": SID[:8], "text": "dir"})),
        ("bad base64", plaintext),
        ("bad base64", b"!!!!\n"),
    ]
    for _, line in cases:
        await dev.send_line(line)
    await dev.send_line(nxt)
    await asyncio.sleep(0.1)
    assert [e["text"] for e in hub.table.pop_events(SID)] == ["first", "second"]
    drops = [r.getMessage() for r in caplog.records if "drop frame" in r.getMessage()]
    assert len(drops) == len(cases)
    for (why, line), msg in zip(cases, drops):
        assert why in msg and repr(line[:48]) in msg
    task.cancel()


@aio
async def test_garbage_before_hello_is_ignored(caplog):
    caplog.set_level(logging.WARNING, logger="cardbuddy.ble_link")
    hub = Hub()
    link = Link(KEY, hub)
    hub.link = link
    conn = FakeConn()
    task = asyncio.create_task(link.serve(conn))
    nh = crypto.parse_hello(await conn.to_dev.get(), crypto.ROLE_HOST)
    await conn.to_host.put(b"junk\n")
    await conn.to_host.put(crypto.hello(crypto.ROLE_HOST, nh))
    nd = os.urandom(16)
    await conn.to_host.put(crypto.hello(crypto.ROLE_DEVICE, nd))
    dev = crypto.Session(*crypto.hkdf(KEY, nh, nd), crypto.DIR_D2H)
    assert dev.open(await asyncio.wait_for(conn.to_dev.get(), 2))["t"] == "sessions"
    assert sum("drop hello" in r.getMessage() for r in caplog.records) == 2
    task.cancel()


@aio
async def test_hello_timeout(monkeypatch):
    monkeypatch.setattr(ble_link, "HELLO_TIMEOUT", 0.2)
    link = Link(KEY, Hub())
    assert await asyncio.wait_for(link.serve(FakeConn()), 2) is False
    assert not link.connected


@aio
async def test_oversized_message_is_logged_and_skipped(caplog):
    hub, link, conn, dev, task = await start()
    await dev.recv()
    link.send({"t": "ask", "n": 1, "req": "r1", "qs": [{"q": "あ" * 800, "h": "", "o": [], "m": False}]})
    link.send({"t": "resolved", "req": "r1", "by": "abort"})
    assert await dev.recv() == {"t": "resolved", "req": "r1", "by": "abort"}
    assert any("plaintext too long" in r.getMessage() for r in caplog.records)
    task.cancel()


def test_line_buffer_reassembles_and_drops_overlong():
    b = LineBuffer()
    line = b"A" * 50
    out = []
    for i in range(0, 51, 20):
        out += b.feed((line + b"\n")[i:i + 20])
    assert out == [line]
    assert b.feed(b"x" * 4096 + b"\nok\n") == [b"x" * 4096, b"ok"]
    assert b.feed(b"y" * 3000) == []
    assert b.feed(b"y" * 3000) == []
    assert b.feed(b"y" * 10 + b"\nnext") == []
    assert b.feed(b"\n") == [b"next"]
    assert b.feed(b"z" * 4097 + b"\nafter\n") == [b"after"]


class FakeClient:
    def __init__(self, mtu):
        self.mtu_size, self.writes = mtu, []

    async def write_gatt_char(self, uuid, data, response):
        assert uuid == ble_link.NUS_RX
        self.writes.append(bytes(data))


@pytest.mark.parametrize("mtu,size", [(517, 180), (23, 20)])
@aio
async def test_ble_conn_splits_writes(mtu, size):
    conn = ble_link.BleConn()
    conn.client = FakeClient(mtu)
    line = b"Q" * 400 + b"\n"
    await conn.send_line(line)
    assert b"".join(conn.client.writes) == line
    assert max(map(len, conn.client.writes)) == size
    conn.on_notify(None, bytearray(b"ab"))
    conn.on_notify(None, bytearray(b"c\n"))
    assert await conn.recv_line() == b"abc"
    conn.on_disconnect(None)
    with pytest.raises(ConnectionError):
        await conn.recv_line()


@aio
async def test_reconnect_backoff(monkeypatch):
    results = iter([False, False, False, True, False, False, False, False, False])
    delays = []

    async def once(self, name):
        return next(results)

    async def sleep(d):
        delays.append(d)
        if len(delays) == 9:
            raise asyncio.CancelledError

    monkeypatch.setattr(Link, "_connect_once", once)
    monkeypatch.setattr(ble_link.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await Link(KEY, Hub()).run(None)
    assert delays == [2, 4, 8, 2, 4, 8, 16, 30, 30]


def test_name_match():
    assert ble_link.name_matches("Claude_ab12cd", None)
    assert not ble_link.name_matches("Other", None)
    assert not ble_link.name_matches(None, None)
    assert ble_link.name_matches("Claude_ab12cd", "Claude_ab12cd")
    assert not ble_link.name_matches("Claude_ffffff", "Claude_ab12cd")


@aio
async def test_unencodable_message_does_not_kill_the_link(caplog):
    hub, link, conn, dev, task = await start()
    await dev.recv()
    link.send({"t": "sessions", "s": [{"n": 1, "id": "x", "name": "x", "title": "\ud83d", "state": "idle"}]})
    link.send({"t": "resolved", "req": "r1", "by": "abort"})
    assert await dev.recv() == {"t": "resolved", "req": "r1", "by": "abort"}
    assert not task.done()
    task.cancel()


class _Dev:
    def __init__(self, name, address="AA"):
        self.name, self.address = name, address


class _Adv:
    def __init__(self, local_name):
        self.local_name = local_name


@pytest.mark.parametrize("cached,adv,want,ok", [
    ("MPY ESP32", "Claude_A1B2C3", None, True),
    ("MPY ESP32", "Claude_A1B2C3", "Claude_A1B2C3", True),
    ("MPY ESP32", None, None, False),
    ("Claude_old", "Other", None, False),
    ("MPY ESP32", "Claude_A1B2C3", "Claude_ffffff", False),
])
@aio
async def test_connect_uses_advertised_name(monkeypatch, cached, adv, want, ok):
    seen = {}

    async def find(filt, timeout):
        d = _Dev(cached)
        return d if filt(d, _Adv(adv)) else None

    class Client:
        mtu_size = 185

        def __init__(self, dev, disconnected_callback):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def start_notify(self, uuid, cb):
            pass

    async def serve(self, conn):
        seen["device"] = self.device
        return True

    monkeypatch.setattr(ble_link.BleakScanner, "find_device_by_filter", find)
    monkeypatch.setattr(ble_link, "BleakClient", Client)
    monkeypatch.setattr(Link, "serve", serve)
    link = Link(KEY, Hub())
    assert await link._connect_once(want) is ok
    assert seen.get("device") == (adv if ok else None)
    assert link.device is None
