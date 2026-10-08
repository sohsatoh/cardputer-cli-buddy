"""buddyd: mod の HTTP API と Cardputer への BLE リンクを結線する常駐デーモン。"""

import argparse
import asyncio
import contextlib
import fcntl
import logging
import os
import re
import resource
import signal
import stat
import sys
import time
from pathlib import Path

from . import http_api, voice, web, webpki, wifi
from .ble_link import Link
from .session_table import PROMPT_MAX, SessionTable, trunc

log = logging.getLogger("buddyd")

SESSIONS_DEBOUNCE = 0.5
EXPIRE_INTERVAL = 5.0
# 再接続直後に溜まった要求を全部流すと、デバイスの受信キューと画面が古い要求で埋まる
RESEND_MAX = 8
WEB_QUEUE = 256
# Web に流すデバイス向けメッセージ（perm / ask は全文の版を web_new_req で別に流す）
WEB_FORWARD = ("resolved", "ack_prompt")
VID = re.compile(r"[A-Za-z0-9]{1,8}")
LANGS = ("ja-JP", "en-US")


class Hub:
    def __init__(self, link=None, stt=None):
        self.link = link
        # 未指定なら cardbuddy.stt を使う、テストでは transcribe と SttError を持つ偽物を渡す
        self.stt = stt
        self.voice = voice.Recorder()
        # stt は子プロセスを最大 180 秒走らせるので、文字起こしは常に 1 本だけにする
        self.voice_task: asyncio.Task | None = None
        self._voice_latest: str | None = None
        self.table = SessionTable()
        self.polls: dict[str, int] = {}
        self.wake_event = asyncio.Event()
        self._kick = asyncio.Event()
        self._last_sessions = None
        self.web_subs: set[asyncio.Queue] = set()
        self.web_enabled = False
        self._last_web_sessions = None
        self._activity_subs: list = []
        self.subscribe_activity(lambda sid, item: self._web_publish({"t": "activity", "sid": sid, "item": item}))

    def subscribe_activity(self, cb):
        """cb(sid, item) を新しい activity ごとに呼ぶ。戻り値を呼ぶと購読をやめる。"""
        self._activity_subs.append(cb)
        return lambda: self._activity_subs.remove(cb)

    def publish_activity(self, sid: str, item: dict):
        for cb in list(self._activity_subs):
            try:
                cb(sid, item)
            except Exception:
                log.exception("activity subscriber failed")

    def activity(self, sid: str) -> dict:
        return {"events": self.table.activity(sid), "running": self.table.running_tools(sid)}

    def send(self, msgs: list[dict]):
        for m in msgs:
            self.link.send(m)
            if m["t"] == "sessions":
                self._publish_web_sessions()
            elif m["t"] in WEB_FORWARD:
                self._web_publish(m)
        # 表の変化は必ずデバイス宛てのメッセージを伴うので、ここで sessions の再送と待ちの起床をまとめて行う
        if msgs:
            self.kick()
            self.wake()

    def web_subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(WEB_QUEUE)
        self.web_subs.add(q)
        return q

    def web_unsubscribe(self, q: asyncio.Queue):
        self.web_subs.discard(q)

    def _publish_web_sessions(self):
        s = self.table.web_sessions()
        if s != self._last_web_sessions:
            self._last_web_sessions = s
            self._web_publish({"t": "sessions", "s": s, "now": time.time()})

    def web_snapshot(self) -> list[dict]:
        # now は iPhone の時計とのずれを補正して経過時間を出すために付ける
        acts = [{"t": "activity", "sid": sid, **self.activity(sid), "now": time.time()} for sid in self.table.sessions]
        return [{"t": "sessions", "s": self.table.web_sessions(), "now": time.time()}, *self.table.web_reqs(), *acts]

    def web_new_req(self, req: str):
        ev = self.table.web_req(req)
        if ev:
            self._web_publish(ev)

    def _web_publish(self, ev: dict):
        for q in list(self.web_subs):
            try:
                q.put_nowait(ev)
            except asyncio.QueueFull:
                # 読まない購読者のために溜め続けない、SSE 側は購読が外れたのを見て接続を閉じる
                self.web_subs.discard(q)

    def wake(self):
        self.wake_event.set()
        self.wake_event = asyncio.Event()

    def kick(self):
        self._kick.set()

    def on_up(self):
        self.on_down()
        self._last_sessions = self.table.sessions_msg()
        self.send([self._last_sessions, *self.table.pending_msgs()[-RESEND_MAX:]])

    def on_down(self):
        self.voice.reset()
        self._voice_latest = None
        self._cancel_voice_task()

    def _cancel_voice_task(self):
        if self.voice_task is not None:
            self.voice_task.cancel()
            self.voice_task = None

    def on_msg(self, msg: dict, by: str = "device"):
        t = msg.get("t")
        if isinstance(t, str) and t.startswith("voice_"):
            self._on_voice(t, msg)
            return
        self.send(self.table.handle_device(msg, by))
        self.wake()

    def on_audio(self, pt: bytes):
        self.voice.audio(pt)

    def _on_voice(self, t: str, msg: dict):
        vid = msg.get("vid")
        if not (isinstance(vid, str) and VID.fullmatch(vid)):
            log.warning("drop %s: bad vid", t)
        elif t == "voice_begin":
            if msg.get("lang") not in LANGS:
                log.warning("drop voice_begin: bad lang")
                return
            self._cancel_voice_task()
            self.voice.begin(vid, msg["lang"])
            self._voice_latest = vid
        elif t == "voice_cancel":
            if vid == self._voice_latest:
                self.voice.reset()
                self._voice_latest = None
                self._cancel_voice_task()
        elif t == "voice_end":
            rec = self.voice.end(vid)
            if rec is None:
                log.info("drop voice_end for %s: not recording", vid)
                return
            self._cancel_voice_task()
            self.voice_task = asyncio.create_task(self._transcribe(vid, *rec))
        else:
            log.warning("drop device message: unknown t=%r", t)

    async def _transcribe(self, vid: str, ulaw: bytes, lang: str):
        secs = len(ulaw) / voice.RATE
        t0 = time.monotonic()
        stt, path = self.stt, None
        if not ulaw:
            msg = {"t": "voice_error", "vid": vid, "err": "no audio"}
        else:
            try:
                if stt is None:
                    from . import stt
                path = voice.write_wav(home() / "tmp", ulaw)
                text = await stt.transcribe(str(path), lang)
                msg = {"t": "voice_text", "vid": vid, "text": trunc(text, PROMPT_MAX)}
                log.info("voice %s: %.1fs audio transcribed in %.1fs", vid, secs, time.monotonic() - t0)
            except Exception as e:
                # 例外の文言に認識結果が混ざりうるので、ログには型名だけを出す
                log.warning("voice %s: %.1fs audio, transcription failed in %.1fs (%s)",
                            vid, secs, time.monotonic() - t0, type(e).__name__)
                known = stt is not None and isinstance(e, stt.SttError) and str(e)
                msg = {"t": "voice_error", "vid": vid, "err": trunc(str(e), 80) if known else "transcription failed"}
            finally:
                if path is not None:
                    path.unlink(missing_ok=True)
        if self._voice_latest == vid:
            self.send([msg])
        else:
            log.info("voice %s: result dropped, a newer recording or session replaced it", vid)

    async def sessions_loop(self):
        while True:
            await self._kick.wait()
            await asyncio.sleep(SESSIONS_DEBOUNCE)
            self._kick.clear()
            msg = self.table.sessions_msg()
            if msg != self._last_sessions:
                self._last_sessions = msg
                self.send([msg])
            # updated_at や番号の無いセッションは Cardputer 向けの sessions に出ないので、Web は別に差分を見る
            self._publish_web_sessions()

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


async def _start_web(hub, port: int):
    if not web.pki_ready():
        log.info("web disabled: no certificates in %s (run `buddy web init`)", webpki.pki_dir())
        return
    # Web 用のファイルが壊れていても、デバイスとの経路は止めない
    try:
        for w in webpki.server_status()["warnings"]:
            log.warning("web: %s", w)
        await web.serve_web(hub, "0.0.0.0", port)
    except Exception as e:
        log.error("web disabled: %r", e)
    else:
        log.info("web UI on https://0.0.0.0:%d/ (mTLS)", port)


async def _amain(key: bytes, name: str | None, tcp_port: int | None = None, web_port: int | None = None):
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    # 文字起こし中に SIGKILL やクラッシュで落ちると finally が走らず WAV が残る
    for wav in (h / "tmp").glob("*.wav"):
        wav.unlink(missing_ok=True)
    # SIGTERM の既定動作は即終了なので、メインを cancel して asyncio.run に残りのタスクの finally を走らせる
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, asyncio.current_task().cancel)
    # socket の connect 確認だけでは同時起動の 2 本目が生きている socket を消して奪えてしまう
    lock = lock_or_exit(h)  # noqa: F841  プロセス終了まで保持する
    hub = Hub()
    link = hub.link = Link(key, hub)
    sock = h / "buddyd.sock"
    server = await http_api.serve(hub, str(sock))
    log.info("listening on %s", sock)
    tasks = [link.run(name), hub.sessions_loop(), hub.expire_loop()]
    if tcp_port is not None:
        try:
            await wifi.serve_tcp(link, "0.0.0.0", tcp_port)
        except OSError as e:
            log.error("wifi disabled: cannot listen on tcp port %d (%s)", tcp_port, e)
        else:
            log.info("listening on tcp 0.0.0.0:%d, beaconing on udp %d", tcp_port, wifi.BEACON_PORT)
            tasks.append(wifi.beacon_loop(tcp_port))
    if web_port is not None:
        await _start_web(hub, web_port)
    async with server:
        await asyncio.gather(*tasks)


def main():
    ap = argparse.ArgumentParser(prog="buddyd")
    ap.add_argument("--name", help="exact advertised name of the device "
                    "(default: the name saved by `buddy pair`, else any Claude_*)")
    ap.add_argument("--tcp-port", type=int, default=wifi.TCP_PORT, help="TCP port for Wi-Fi (default: %(default)s)")
    ap.add_argument("--no-wifi", action="store_true", help="BLE only: do not listen on TCP or send beacons")
    ap.add_argument("--no-web", action="store_true", help="do not serve the LAN web UI")
    args = ap.parse_args()
    logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    key = load_key(home() / "key")
    # launchd の既定の fd 上限は小さく、LAN からの接続で Unix socket の accept まで止まりうる
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    with contextlib.suppress(ValueError, OSError):
        resource.setrlimit(resource.RLIMIT_NOFILE, (min(max(soft, 4096), hard), hard))
    name = resolve_name(args.name)
    log.info("looking for %s", name or "any Claude_* device")
    try:
        asyncio.run(_amain(key, name, None if args.no_wifi else args.tcp_port, None if args.no_web else web.WEB_PORT))
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
