"""PROTOCOL.md のデバイス側：Hello ハンドシェイク、Data の受信と dispatch、返信の送信。

BLE にも UI にも依存しない。リンク（BLE / TCP、send_line と hello_failed を持つ）から受けた
1 行を on_line(line, link) に渡す。セッションを確立したリンクにだけ返信する。
UI は sessions / queue / last_ack / log を読み、rev が変わったら描き直す。
"""

import time
from os import urandom

import crypto

KEY_PATH = "/flash/cardbuddy.key"
MAX_PROMPT = 500
MAX_QUEUE = 16
INBOX_BYTES = 16384
HELLO_MS = 5000
TX_MAX_LINES = 4  # 送信待ちの行数の上限。lwIP / Wi-Fi の送信バッファに上限が無く、積むと IDF ヒープが尽きるため


def ms():
    t = getattr(time, "ticks_ms", None)
    return t() if t else int(time.monotonic() * 1000)  # CPython（テスト）


def since(t0):
    d = getattr(time, "ticks_diff", None)
    return d(ms(), t0) if d else ms() - t0


def _sleep_ms(n):
    s = getattr(time, "sleep_ms", None)
    if s:
        s(n)
    else:
        time.sleep(n / 1000)


class TxQueue:
    """行の送信 FIFO（BLE と TCP で共用）。

    write(mv) は送れた byte 数を返し、0 なら詰まり（ENOMEM / EAGAIN）、例外なら失敗。
    送りかけの行を捨てると相手側で次の行とつながって両方壊れるので、詰まったら同じ位置から再開する。
    """

    def __init__(self, write):
        self._write = write
        self._q = []
        self._off = 0

    def clear(self):
        self._q = []
        self._off = 0

    def idle(self):
        return not self._q

    def put(self, line):
        """積めたら True。TX_MAX_LINES 行たまっていたら積まずに False。"""
        if len(self._q) >= TX_MAX_LINES:
            return False
        self._q.append(line)
        return True

    def pump(self):
        """送れるだけ送る。送り切れば True、詰まれば False、失敗なら None（キューは捨てる）。"""
        while self._q:
            line = self._q[0]
            mv = memoryview(line)
            while self._off < len(line):
                try:
                    n = self._write(mv[self._off :])
                except OSError as e:
                    print("txq: write failed:", e)
                    self.clear()
                    return None
                if not n:
                    return False
                self._off += n
            self._q.pop(0)
            self._off = 0
        return True

    def send(self, line, timeout_ms=2000):
        """積んで、キューが空くまで送る。

        JSON は捨てない。キューが一杯なら空くまで待ち、時間内に空かなければ上限を超えて後ろに積む。
        時間内に送り切れなくても、行はキューに残して pump() に任せる。
        """
        t0 = ms()
        queued = False
        while True:
            if not queued:
                queued = self.put(line)
            r = self.pump()
            if r is None:
                return False
            if queued and r:
                return True
            if since(t0) > timeout_ms:
                if not queued:
                    self._q.append(line)
                print("txq: still busy, leave it to pump()")
                return True
            _sleep_ms(1)


class _FnLink:
    kind = "BLE"

    def __init__(self, send_line):
        self.send_line = send_line

    def hello_failed(self):
        pass


def inbox_push(inbox, line, limit=INBOX_BYTES):
    """受信行を inbox に足す。合計が limit を超えるなら古い行から捨てる。"""
    total = len(line)
    for x in inbox:
        total += len(x)
    while inbox and total > limit:
        total -= len(inbox.pop(0))
        print("buddy_protocol: inbox full, drop oldest line")
    inbox.append(line)


def load_key(path=KEY_PATH):
    try:
        with open(path, "rb") as f:
            key = f.read()
    except OSError:
        return None
    if len(key) != 32:
        print("buddy_protocol: key file has", len(key), "bytes, want 32")
        return None
    return key


def new_vid():
    return "".join("%02x" % b for b in urandom(4))


def _clip(s, n):
    return s if len(s) <= n else s[: n - 1] + "…"


def _check(msg):
    t = msg["t"]
    if t in ("perm", "ask", "resolved") and not isinstance(msg.get("req"), str):
        raise ValueError("req")
    if t == "ask":
        qs = msg["qs"]
        if not isinstance(qs, list) or not 1 <= len(qs) <= 4:
            raise ValueError("qs")
        for q in qs:
            if not isinstance(q["o"], list) or not 2 <= len(q["o"]) <= 4:
                raise ValueError("o")
    if t == "sessions" and not isinstance(msg["s"], list):
        raise ValueError("s")
    if t in ("voice_text", "voice_error"):
        if not isinstance(msg.get("vid"), str) or not isinstance(msg.get("text" if t == "voice_text" else "err"), str):
            raise ValueError("voice")
    if t == "log":
        if not isinstance(msg.get("p", 0), int):
            raise ValueError("p")
        for it in msg["items"]:
            if not isinstance(it["x"], str):
                raise ValueError("x")


class Protocol:
    def __init__(self, key, send_line=None):
        self.key = key
        self._default = _FnLink(send_line) if send_line else None
        self.session = None
        self.link = None  # セッションを確立したリンク
        self._pending = []  # [link, nh, nd, Hello(d) を送った時刻]
        self.sessions = []
        self.queue = []  # 到着順の perm / ask メッセージ
        self.last_ack = None
        self.log = None  # 最後に届いた log 1 件だけを持つ（実機の空きメモリが少ない）
        self.voice = None  # 最後に届いた voice_text / voice_error
        self.rev = 0

    @property
    def paired(self):
        return self.key is not None

    @property
    def ready(self):
        return self.session is not None

    def _reset(self):
        self.session = None
        self.link = None
        self.sessions = []
        self.queue = []
        self.last_ack = None
        self.log = None
        self.voice = None
        self.rev += 1

    def on_disconnect(self, link=None):
        link = link or self._default
        self._pending = [p for p in self._pending if p[0] is not link]
        if link is self.link:
            self._reset()

    def tick(self):
        """Hello(d) を送ってから HELLO_MS 以内に Hello(k) が来なかったリンクを、失敗として知らせる。"""
        for p in self._pending:
            if since(p[3]) > HELLO_MS:
                self._pending.remove(p)
                print("buddy_protocol: hello timed out")
                p[0].hello_failed()
                return

    # ----- inbound

    def on_line(self, line, link=None):
        link = link or self._default
        if not self.paired:
            print("buddy_protocol: drop: not paired")
            return
        try:
            raw = crypto.decode_line(line)
        except crypto.FrameError as e:
            print("buddy_protocol: drop:", e)
            return
        if len(raw) < 2 or raw[0] != crypto.VER:
            print("buddy_protocol: drop: bad frame")
            return
        if raw[1] == crypto.HELLO:
            self._on_hello(raw, link)
            return
        if self.session is None or link is not self.link:
            print("buddy_protocol: drop: no session on this link")
            return
        try:
            msg = self.session.open_raw(raw)
            _check(msg)
        except crypto.FrameError as e:
            print("buddy_protocol: drop:", e)
            return
        except (KeyError, TypeError, ValueError) as e:
            print("buddy_protocol: drop: malformed", repr(e))
            return
        self._dispatch(msg)

    def _on_hello(self, raw, link):
        if len(raw) != 19 or raw[2] not in (crypto.ROLE_HOST, crypto.ROLE_ACK):
            print("buddy_protocol: drop: bad hello")
            return
        pend = None
        for p in self._pending:
            if p[0] is link:
                pend = p
        if pend is not None:
            self._pending.remove(pend)
        if raw[2] == crypto.ROLE_HOST:
            # 今のセッションは Hello(k) で鍵を確かめるまで残す。鍵を持たない相手の Hello(h) だけで捨てさせないため
            nh, nd = raw[3:], urandom(16)
            self._pending.append([link, nh, nd, ms()])
            link.send_line(crypto.hello_device(self.key, nh, nd))
            return
        if pend is None:
            print("buddy_protocol: drop: hello ack without hello")
            return
        _, nh, nd, _ = pend
        if not crypto._eq(raw[3:], crypto.hello_tag(self.key, b"h", nh, nd)):
            print("buddy_protocol: drop: bad hello ack tag")
            link.hello_failed()
            return
        self._reset()
        enc, mac = crypto.hkdf(self.key, nh, nd)
        self.session = crypto.Session(enc, mac, crypto.DIR_D2H)
        self.link = link

    def _dispatch(self, msg):
        t = msg["t"]
        if t == "sessions":
            self.sessions = [x for x in msg["s"][:9] if isinstance(x, dict)]
        elif t in ("perm", "ask"):
            if self._find(msg["req"]) is None:
                self.queue.append(msg)
                if len(self.queue) > MAX_QUEUE:
                    print("buddy_protocol: queue full, drop", self.queue.pop(0)["req"])
        elif t == "resolved":
            self._drop(msg["req"])
        elif t == "ack_prompt":
            self.last_ack = msg
        elif t == "ping":
            # 送信が詰まっていても待たない（録音中にメインループを止めると Mic のバッファがあふれる）
            self._send({"t": "pong"}, wait=False)
            return
        elif t == "log":
            self.log = msg
        elif t in ("voice_text", "voice_error"):
            self.voice = msg
        else:
            print("buddy_protocol: drop: unknown t", t)
            return
        self.rev += 1

    def _find(self, req):
        for m in self.queue:
            if m["req"] == req:
                return m
        return None

    def _drop(self, req):
        self.queue = [m for m in self.queue if m["req"] != req]

    # ----- outbound

    def _send(self, msg, wait=True):
        if self.session is None:
            return False
        try:
            line = self.session.seal(msg)
        except crypto.FrameError as e:
            print("buddy_protocol: send failed:", e)
            return False
        if not wait and hasattr(self.link, "enqueue"):
            return self.link.enqueue(line) is not False
        return self.link.send_line(line) is not False

    def _reply(self, req, msg):
        if self._find(req) is None:
            return False
        ok = self._send(msg)
        if ok:
            self._drop(req)
            self.rev += 1
        return ok

    def reply_perm(self, req, decision):
        m = self._find(req)
        if decision == "allow" and (m is None or m.get("full") is not True):
            return False
        return self._reply(req, {"t": "perm_reply", "req": req, "decision": decision})

    def reply_ask(self, req, answers):
        return self._reply(req, {"t": "ask_reply", "req": req, "answers": answers})

    def send_prompt(self, n, sid, text):
        return self._send({"t": "prompt", "n": n, "id": sid, "text": _clip(text, MAX_PROMPT)})

    def request_log(self, n, sid, p=0):
        return self._send({"t": "log_req", "n": n, "id": sid, "p": p})

    def take_log(self):
        """届いた log を返して手放す。UI が折り返した行だけを持ち、元の本文は残さないため。"""
        log, self.log = self.log, None
        return log

    def take_voice(self):
        v, self.voice = self.voice, None
        return v

    def voice_begin(self, vid, lang):
        return self._send({"t": "voice_begin", "vid": vid, "lang": lang})

    def voice_end(self, vid):
        return self._send({"t": "voice_end", "vid": vid})

    def voice_cancel(self, vid):
        return self._send({"t": "voice_cancel", "vid": vid})

    def seal_audio(self, pt, bufs=None):
        """seq と μ-law の平文を Audio フレームの 1 行にする。送るのは呼び出し側（音声は非同期に送るため）。

        bufs（crypto.seal_buffers）を渡すと、その中に作って memoryview を返す。
        """
        if self.session is None:
            return None
        try:
            if bufs is not None:
                return self.session.seal_into(pt, crypto.AUDIO, bufs)
            return self.session.seal_bytes(pt, crypto.AUDIO)
        except crypto.FrameError as e:
            print("buddy_protocol: seal failed:", e)
            return None
