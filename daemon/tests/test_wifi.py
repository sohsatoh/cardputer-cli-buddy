import asyncio
import functools
import os

import pytest

from cardbuddy import ble_link, crypto, wifi
from cardbuddy.ble_link import Link
from cardbuddy.buddyd import Hub

KEY = bytes(range(32))


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


async def server():
    hub = Hub()
    link = hub.link = Link(KEY, hub)
    srv = await wifi.serve_tcp(link, "127.0.0.1", 0)
    return link, srv, srv.sockets[0].getsockname()[1]


async def device(port, key=KEY):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    nh = crypto.parse_hello(await asyncio.wait_for(r.readline(), 2), crypto.ROLE_HOST)
    nd = os.urandom(16)
    w.write(crypto.hello_device(key, nh, nd))
    await w.drain()
    return r, w, nh, nd


async def established(port):
    r, w, nh, nd = await device(port)
    crypto.check_hello_ack(await asyncio.wait_for(r.readline(), 2), KEY, nh, nd)
    s = crypto.Session(*crypto.hkdf(KEY, nh, nd), crypto.DIR_D2H)
    assert s.open(await asyncio.wait_for(r.readline(), 2))["t"] == "sessions"
    return r, w, s


@aio
async def test_tcp_session_over_localhost():
    link, srv, port = await server()
    r, w, s = await established(port)
    assert link.connected and link.transport == "tcp" and link.device == "127.0.0.1"
    w.write(s.seal({"t": "pong"}))
    await w.drain()
    w.close()
    for _ in range(40):
        if not link.connected:
            break
        await asyncio.sleep(0.05)
    assert not link.connected
    srv.close()


@aio
async def test_tcp_without_key_cannot_establish():
    link, srv, port = await server()
    r, w, _, _ = await device(port, key=bytes(32))
    assert await asyncio.wait_for(r.read(), 2) == b""
    assert not link.connected
    srv.close()


@aio
async def test_tcp_handshake_timeout(monkeypatch):
    monkeypatch.setattr(ble_link, "HELLO_TIMEOUT", 0.2)
    link, srv, port = await server()
    r, w = await asyncio.open_connection("127.0.0.1", port)
    assert (await asyncio.wait_for(r.read(), 2)).count(b"\n") == 1
    srv.close()


@aio
async def test_pending_tcp_connections_are_capped():
    link, srv, port = await server()
    idle = []
    for _ in range(wifi.MAX_PENDING):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        await asyncio.wait_for(r.readline(), 2)
        idle.append(w)
    r, w = await asyncio.open_connection("127.0.0.1", port)
    assert await asyncio.wait_for(r.read(), 2) == b""
    idle.pop().close()
    await asyncio.sleep(0.1)
    await established(port)
    assert link.transport == "tcp"
    srv.close()


@aio
async def test_only_one_tcp_session():
    link, srv, port = await server()
    r1, w1, _ = await established(port)
    r2, w2, _ = await established(port)
    assert await asyncio.wait_for(r1.read(), 2) == b""
    assert link.connected
    srv.close()


def test_parse_broadcasts():
    text = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
bridge0: flags=8863<UP> mtu 1500
\tinet 10.0.0.1 netmask 0xffff0000 broadcast 10.0.255.255
\tinet 192.168.1.21 netmask 0xffffff00 broadcast 192.168.1.255
"""
    assert wifi.parse_broadcasts(text) == ["192.168.1.255", "10.0.255.255"]


class FakeSock:
    def __init__(self):
        self.sent = []
        self.closed = False

    def sendto(self, data, addr):
        if addr[0] == "10.9.9.255":
            raise OSError("unreachable")
        self.sent.append((data, addr))

    def close(self):
        self.closed = True


@aio
async def test_beacon_destinations_and_payload(monkeypatch):
    monkeypatch.setattr(wifi, "BEACON_INTERVAL", 0.02)

    async def addrs():
        return ["192.168.1.255", "10.9.9.255"]

    monkeypatch.setattr(wifi, "broadcast_addrs", addrs)
    sock = FakeSock()
    task = asyncio.create_task(wifi.beacon_loop(47823, sock))
    await asyncio.sleep(0.07)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert sock.closed and len(sock.sent) >= 4
    assert set(sock.sent) == {(b"cardbuddy/1 47823", ("255.255.255.255", 47824)),
                              (b"cardbuddy/1 47823", ("192.168.1.255", 47824))}


@aio
async def test_broadcast_addrs_from_ifconfig():
    addrs = await wifi.broadcast_addrs()
    assert all(a.count(".") == 3 for a in addrs)
