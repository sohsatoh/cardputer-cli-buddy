"""Wi-Fi のリンク（PROTOCOL.md の Wi-Fi（TCP））：TCP の待ち受けと UDP ビーコン。"""

import asyncio
import contextlib
import itertools
import logging
import re
import socket
from collections import deque

from .ble_link import LineBuffer

log = logging.getLogger(__name__)

TCP_PORT = 47823
BEACON_PORT = 47824
BEACON_INTERVAL = 2.0
# ponytail: ブロードキャスト先は ifconfig から読み、この回数ごとに読み直す（Wi-Fi の切り替えに追従する程度）
BEACON_REFRESH = 15
MAX_PENDING = 4


class TcpConn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.r, self.w = reader, writer
        self.buf = LineBuffer()
        self.lines: deque[bytes] = deque()

    async def send_line(self, line: bytes):
        self.w.write(line)
        await self.w.drain()

    async def recv_line(self) -> bytes:
        while not self.lines:
            data = await self.r.read(4096)
            if not data:
                raise ConnectionError("closed by peer")
            self.lines.extend(self.buf.feed(data))
        return self.lines.popleft()


async def serve_tcp(link, host: str, port: int) -> asyncio.base_events.Server:
    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else "?"
        try:
            if link.pending["tcp"] >= MAX_PENDING:
                log.warning("tcp: refused %s, %d handshakes already pending", ip, MAX_PENDING)
                return
            await link.serve(TcpConn(reader, writer), "tcp", ip)
        except (ConnectionError, OSError) as e:
            log.info("tcp %s: %r", ip, e)
        finally:
            writer.close()
            with contextlib.suppress(ConnectionError, OSError):
                await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


def parse_broadcasts(ifconfig: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"\bbroadcast (\d+\.\d+\.\d+\.\d+)", ifconfig)))


async def broadcast_addrs() -> list[str]:
    # stdlib にはインターフェースのブロードキャストアドレスを取る手段が無い
    try:
        p = await asyncio.create_subprocess_exec(
            "/sbin/ifconfig", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await p.communicate()
    except OSError as e:
        log.debug("ifconfig: %s", e)
        return []
    return parse_broadcasts(out.decode(errors="replace"))


async def beacon_loop(tcp_port: int, sock: socket.socket | None = None):
    payload = f"cardbuddy/1 {tcp_port}".encode()
    if sock is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setblocking(False)
    dests: list[str] = []
    try:
        for n in itertools.count():
            if n % BEACON_REFRESH == 0:
                dests = list(dict.fromkeys(["255.255.255.255", *await broadcast_addrs()]))
            for d in dests:
                try:
                    sock.sendto(payload, (d, BEACON_PORT))
                except OSError as e:
                    log.debug("beacon to %s: %s", d, e)
            await asyncio.sleep(BEACON_INTERVAL)
    finally:
        sock.close()
