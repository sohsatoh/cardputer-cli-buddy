"""buddyd: mod の HTTP API と Cardputer への BLE リンクを結線する常駐デーモン。"""

import argparse
import asyncio
import fcntl
import logging
import os
import stat
import sys
import time
from pathlib import Path

from . import http_api
from .ble_link import Link
from .session_table import SessionTable

log = logging.getLogger("buddyd")

SESSIONS_DEBOUNCE = 0.5
EXPIRE_INTERVAL = 5.0
# 再接続直後に溜まった要求を全部流すと、デバイスの受信キューと画面が古い要求で埋まる
RESEND_MAX = 8


class Hub:
    def __init__(self, link=None):
        self.link = link
        self.table = SessionTable()
        self.polls: dict[str, int] = {}
        self.wake_event = asyncio.Event()
        self._kick = asyncio.Event()
        self._last_sessions = None

    def send(self, msgs: list[dict]):
        for m in msgs:
            self.link.send(m)
        # 表の変化は必ずデバイス宛てのメッセージを伴うので、ここで sessions の再送と待ちの起床をまとめて行う
        if msgs:
            self.kick()
            self.wake()

    def wake(self):
        self.wake_event.set()
        self.wake_event = asyncio.Event()

    def kick(self):
        self._kick.set()

    def on_up(self):
        self._last_sessions = self.table.sessions_msg()
        self.send([self._last_sessions, *self.table.pending_msgs()[-RESEND_MAX:]])

    def on_msg(self, msg: dict):
        self.send(self.table.handle_device(msg))
        self.wake()

    async def sessions_loop(self):
        while True:
            await self._kick.wait()
            await asyncio.sleep(SESSIONS_DEBOUNCE)
            self._kick.clear()
            msg = self.table.sessions_msg()
            if msg != self._last_sessions:
                self._last_sessions = msg
                self.send([msg])

    async def expire_loop(self):
        while True:
            await asyncio.sleep(EXPIRE_INTERVAL)
            out = self.table.expire(time.monotonic())
            if out:
                self.send(out)
                self.kick()


def home() -> Path:
    return Path(os.environ.get("CARDBUDDY_HOME") or Path.home() / ".cardbuddy")


def load_key(path: Path) -> bytes:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        raise SystemExit(f"buddyd: key not found: {path} (run `buddy pair --port <serial>` first)") from None
    if mode != 0o600:
        raise SystemExit(f"buddyd: {path} must be mode 0600 (is {mode:04o}); run `chmod 600 {path}`")
    key = path.read_bytes()
    if len(key) != 32:
        raise SystemExit(f"buddyd: {path} must be 32 raw bytes (is {len(key)})")
    return key


def lock_or_exit(h: Path):
    f = open(h / "buddyd.lock", "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        f.close()
        raise SystemExit(f"buddyd: already running (lock held on {h / 'buddyd.lock'})") from None
    return f


def resolve_name(name: str | None) -> str | None:
    if name:
        return name
    try:
        return (home() / "device").read_text().strip() or None
    except FileNotFoundError:
        return None


async def _amain(key: bytes, name: str | None):
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    # socket の connect 確認だけでは同時起動の 2 本目が生きている socket を消して奪えてしまう
    lock = lock_or_exit(h)  # noqa: F841  プロセス終了まで保持する
    hub = Hub()
    link = hub.link = Link(key, hub)
    sock = h / "buddyd.sock"
    server = await http_api.serve(hub, str(sock))
    log.info("listening on %s", sock)
    async with server:
        await asyncio.gather(link.run(name), hub.sessions_loop(), hub.expire_loop())


def main():
    ap = argparse.ArgumentParser(prog="buddyd")
    ap.add_argument("--name", help="exact advertised name of the device "
                    "(default: the name saved by `buddy pair`, else any Claude_*)")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    key = load_key(home() / "key")
    name = resolve_name(args.name)
    log.info("looking for %s", name or "any Claude_* device")
    try:
        asyncio.run(_amain(key, name))
    except KeyboardInterrupt:
        pass
