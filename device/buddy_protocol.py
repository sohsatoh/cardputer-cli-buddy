"""PROTOCOL.md のデバイス側：Hello ハンドシェイク、Data の受信と dispatch、返信の送信。

BLE にも UI にも依存しない。BLE 層から受けた 1 行を on_line に渡し、送る行は
コンストラクタで渡した send_line(bytes) に出す。UI は sessions / queue / last_ack / log を読み、
rev が変わったら描き直す。
"""

from os import urandom

import crypto

KEY_PATH = "/flash/cardbuddy.key"
MAX_PROMPT = 500
MAX_QUEUE = 16
INBOX_BYTES = 16384


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
    def __init__(self, key, send_line):
        self.key = key
        self._send_line = send_line
        self.session = None
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
        self.sessions = []
        self.queue = []
        self.last_ack = None
        self.log = None
        self.voice = None
        self.rev += 1

    def on_disconnect(self):
        self._reset()

    # ----- inbound

    def on_line(self, line):
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
            self._on_hello(raw)
            return
        if self.session is None:
            print("buddy_protocol: drop: no session")
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

    def _on_hello(self, raw):
        if len(raw) != 19 or raw[2] != crypto.ROLE_HOST:
            print("buddy_protocol: drop: bad hello")
            return
        nd = urandom(16)
        self._reset()
        enc, mac = crypto.hkdf(self.key, raw[3:], nd)
        self.session = crypto.Session(enc, mac, crypto.DIR_D2H)
        self._send_line(crypto.hello(crypto.ROLE_DEVICE, nd))

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

    def _send(self, msg):
        if self.session is None:
            return False
        try:
            line = self.session.seal(msg)
        except crypto.FrameError as e:
            print("buddy_protocol: send failed:", e)
            return False
        return self._send_line(line) is not False

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

    def seal_audio(self, pt):
        """seq と μ-law の平文を Audio フレームの 1 行にする。送るのは呼び出し側（音声は非同期に送るため）。"""
        if self.session is None:
            return None
        return self.session.seal_bytes(pt, crypto.AUDIO)
