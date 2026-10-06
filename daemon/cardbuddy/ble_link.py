"""Cardputer への BLE リンク（PROTOCOL.md の Transport / Hello / Data）。

Link は「行を送る / 行を受け取る」だけの conn（send_line / recv_line）の上で動き、
BLE 固有の部分は BleConn と Link.run に閉じている。
"""

import asyncio
import logging
import os

from bleak import BleakClient, BleakScanner

from . import crypto

log = logging.getLogger(__name__)

NUS_RX = "6e400002-b5a3-f393-e0a9-e50e24dcca9e"
NUS_TX = "6e400003-b5a3-f393-e0a9-e50e24dcca9e"
NAME_PREFIX = "Claude_"
HELLO_TIMEOUT = 5.0
WRITE_CHUNK = 180
SCAN_TIMEOUT = 10.0
BACKOFF_MIN, BACKOFF_MAX = 2.0, 30.0


def name_matches(name: str | None, want: str | None) -> bool:
    if not name:
        return False
    return name == want if want else name.startswith(NAME_PREFIX)


class LineBuffer:
    def __init__(self):
        self.buf = b""
        self.skip = False

    def feed(self, data: bytes) -> list[bytes]:
        *lines, self.buf = (self.buf + data).split(b"\n")
        out = []
        for line in lines:
            if self.skip:
                self.skip = False
            elif len(line) > crypto.MAX_LINE:
                log.warning("drop line: %d bytes exceeds %d", len(line), crypto.MAX_LINE)
            else:
                out.append(line)
        if len(self.buf) > crypto.MAX_LINE:
            log.warning("drop line: exceeds %d bytes without newline", crypto.MAX_LINE)
            self.buf, self.skip = b"", True
        return out


class BleConn:
    def __init__(self):
        self.client = None
        self.lines: asyncio.Queue = asyncio.Queue()
        self.buf = LineBuffer()

    def on_notify(self, _char, data: bytearray):
        for line in self.buf.feed(bytes(data)):
            self.lines.put_nowait(line)

    def on_disconnect(self, _client):
        self.lines.put_nowait(None)

    async def send_line(self, line: bytes):
        n = min(WRITE_CHUNK, self.client.mtu_size - 3)
        for i in range(0, len(line), n):
            await self.client.write_gatt_char(NUS_RX, line[i:i + n], response=True)

    async def recv_line(self) -> bytes:
        line = await self.lines.get()
        if line is None:
            raise ConnectionError("disconnected")
        return line


class Link:
    def __init__(self, key: bytes, hub):
        self.key = key
        self.hub = hub
        self.device: str | None = None
        self._out: asyncio.Queue | None = None

    @property
    def connected(self) -> bool:
        return self._out is not None

    def send(self, msg: dict):
        # 未接続中は捨ててよい、必要な状態は確立時に hub.on_up が送り直す
        if self._out is not None:
            self._out.put_nowait(msg)

    async def serve(self, conn) -> bool:
        """1 接続分。セッションが確立したかを返す。"""
        nh = os.urandom(16)
        await conn.send_line(crypto.hello(crypto.ROLE_HOST, nh))
        try:
            nd = await asyncio.wait_for(self._hello(conn), HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("no Hello from device within %.0fs", HELLO_TIMEOUT)
            return False
        sess = crypto.Session(*crypto.hkdf(self.key, nh, nd), crypto.DIR_H2D)
        self._out = asyncio.Queue()
        log.info("session established")
        tasks = [asyncio.create_task(self._pump(conn, sess, self._out)),
                 asyncio.create_task(self._recv(conn, sess))]
        try:
            self.hub.on_up()
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                log.info("link closed: %r", t.exception())
        finally:
            self._out = None
            for t in tasks:
                t.cancel()
            self.hub.on_down()
        return True

    async def _hello(self, conn) -> bytes:
        while True:
            line = await conn.recv_line()
            try:
                return crypto.parse_hello(line, crypto.ROLE_DEVICE)
            except crypto.FrameError as e:
                log.warning("drop hello (%s): %r", e, line[:48])

    async def _pump(self, conn, sess, out: asyncio.Queue):
        # seal と write を 1 本のタスクに直列化しないと ctr の順と到着順がずれ、デバイスが replay として捨てる
        while True:
            msg = await out.get()
            try:
                line = sess.seal(msg)
            except (crypto.FrameError, ValueError) as e:
                log.error("cannot send %s: %r", msg.get("t"), e)
                continue
            await conn.send_line(line)

    async def _recv(self, conn, sess):
        while True:
            line = await conn.recv_line()
            try:
                kind, msg = sess.open_frame(line)
            except crypto.FrameError as e:
                log.warning("drop frame (%s): %r", e, line[:48])
                continue
            if kind == crypto.AUDIO:
                self.hub.on_audio(msg)
            else:
                self.hub.on_msg(msg)

    async def _connect_once(self, name: str | None) -> bool:
        try:
            adv_names = {}

            def match(d, ad):
                # macOS は d.name に接続済みデバイスの GAP 名（例 "MPY ESP32"）をキャッシュするので広告名を優先する
                adv_names[d.address] = ad.local_name or d.name
                return name_matches(adv_names[d.address], name)

            dev = await BleakScanner.find_device_by_filter(match, timeout=SCAN_TIMEOUT)
            if dev is None:
                log.info("device not found")
                return False
            conn = BleConn()
            async with BleakClient(dev, disconnected_callback=conn.on_disconnect) as client:
                conn.client = client
                await client.start_notify(NUS_TX, conn.on_notify)
                self.device = adv_names[dev.address]
                log.info("connected to %s (%s)", self.device, dev.address)
                return await self.serve(conn)
        except Exception as e:
            log.warning("ble error: %r", e)
            return False
        finally:
            self.device = None

    async def run(self, name: str | None):
        delay = BACKOFF_MIN
        while True:
            if await self._connect_once(name):
                delay = BACKOFF_MIN
            log.info("reconnect in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)
