"""Cardputer へのリンク（PROTOCOL.md の Transport / Hello / Data）。

Link は「行を送る / 行を受け取る」だけの conn（send_line / recv_line）の上で動く。
BLE 固有の部分は BleConn と Link.run、TCP は wifi.py にある。確立したセッションは常に 1 本。
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
PING_INTERVAL = 10.0
IDLE_TIMEOUT = 30.0


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


class _Active:
    def __init__(self, transport: str, device: str | None):
        self.transport, self.device = transport, device
        self.stop = asyncio.Event()
        self.done = asyncio.Event()


class Link:
    def __init__(self, key: bytes, hub):
        self.key = key
        self.hub = hub
        self.pending = {"ble": 0, "tcp": 0}  # 確立前の接続数
        self._active: _Active | None = None
        self._out: asyncio.Queue | None = None
        self._swap = asyncio.Lock()
        self._tcp_up = asyncio.Event()
        self._tcp_down = asyncio.Event()
        self._tcp_down.set()

    @property
    def connected(self) -> bool:
        return self._out is not None

    @property
    def transport(self) -> str | None:
        return self._active.transport if self._active else None

    @property
    def device(self) -> str | None:
        return self._active.device if self._active else None

    def send(self, msg: dict):
        # 未接続中は捨ててよい、必要な状態は確立時に hub.on_up が送り直す
        if self._out is not None:
            self._out.put_nowait(msg)

    async def serve(self, conn, transport: str = "ble", device: str | None = None) -> bool:
        """1 接続分。セッションが確立したかを返す。"""
        self.pending[transport] += 1
        try:
            keys = await asyncio.wait_for(self._handshake(conn), HELLO_TIMEOUT)
        except asyncio.TimeoutError:
            log.warning("hello failed (no valid Hello from %s device within %.0fs)", transport, HELLO_TIMEOUT)
            return False
        finally:
            self.pending[transport] -= 1
        if keys is None:
            return False
        async with self._swap:
            old = self._active
            if old is not None:
                if transport == "ble" and old.transport == "tcp":
                    log.info("dropping ble session: a tcp session is active")
                    return False
                old.stop.set()
                await old.done.wait()
            me = self._active = _Active(transport, device)
            out = self._out = asyncio.Queue()
            if transport == "tcp":
                self._tcp_down.clear()
                self._tcp_up.set()
        sess = crypto.Session(*crypto.hkdf(self.key, *keys), crypto.DIR_H2D)
        log.info("session established over %s (%s)", transport, device)
        rx = [asyncio.get_running_loop().time()]
        stop = asyncio.create_task(me.stop.wait())
        tasks = [asyncio.create_task(self._pump(conn, sess, out)),
                 asyncio.create_task(self._recv(conn, sess, rx)),
                 asyncio.create_task(self._keepalive(out, rx)), stop]
        try:
            self.hub.on_up()
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                log.info("%s link closed: %s", transport, "replaced" if t is stop else repr(t.exception()))
        finally:
            self._out = self._active = None
            if transport == "tcp":
                self._tcp_up.clear()
                self._tcp_down.set()
            for t in tasks:
                t.cancel()
            self.hub.on_down()
            me.done.set()
        return True

    async def _handshake(self, conn) -> tuple[bytes, bytes] | None:
        nh = os.urandom(16)
        await conn.send_line(crypto.hello(crypto.ROLE_HOST, nh))
        line = await conn.recv_line()
        try:
            nd = crypto.parse_hello_device(line, self.key, nh)
        except crypto.FrameError as e:
            log.warning("hello failed (%s): %r", e, line[:48])
            return None
        await conn.send_line(crypto.hello_ack(self.key, nh, nd))
        return nh, nd

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

    async def _recv(self, conn, sess, rx: list[float]):
        while True:
            line = await conn.recv_line()
            try:
                kind, msg = sess.open_frame(line)
            except crypto.FrameError as e:
                log.warning("drop frame (%s): %r", e, line[:48])
                continue
            rx[0] = asyncio.get_running_loop().time()
            if kind == crypto.AUDIO:
                self.hub.on_audio(msg)
            elif msg.get("t") != "pong":
                self.hub.on_msg(msg)

    async def _keepalive(self, out: asyncio.Queue, rx: list[float]):
        # TCP にはキープアライブが無く、相手が黙って消えても recv が返らない
        while True:
            await asyncio.sleep(PING_INTERVAL)
            idle = asyncio.get_running_loop().time() - rx[0]
            if idle > IDLE_TIMEOUT:
                log.warning("no frame from device for %.0fs", idle)
                return
            out.put_nowait({"t": "ping"})

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
                log.info("connected to %s (%s)", adv_names[dev.address], dev.address)
                return await self.serve(conn, "ble", adv_names[dev.address])
        except Exception as e:
            log.warning("ble error: %r", e)
            return False

    async def run(self, name: str | None):
        delay = BACKOFF_MIN
        while True:
            await self._tcp_down.wait()
            attempt = asyncio.create_task(self._connect_once(name))
            tcp_up = asyncio.create_task(self._tcp_up.wait())
            try:
                await asyncio.wait({attempt, tcp_up}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                tcp_up.cancel()
                if not attempt.done():
                    attempt.cancel()
            if attempt.cancelled() or not attempt.done():
                await asyncio.gather(attempt, return_exceptions=True)
                log.info("tcp session is up; ble paused")
                delay = BACKOFF_MIN
                continue
            if attempt.result():
                delay = BACKOFF_MIN
            log.info("reconnect in %.0fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, BACKOFF_MAX)
