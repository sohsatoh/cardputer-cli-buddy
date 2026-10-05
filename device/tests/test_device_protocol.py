import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))
sys.path.insert(0, str(ROOT / "daemon"))

import buddy_protocol as bp  # noqa: E402
from cardbuddy import crypto as host  # noqa: E402

V = json.loads((ROOT / "testvectors/envelope.json").read_text())
KEY, NH, ND = (bytes.fromhex(V[k]) for k in ("key", "nh", "nd"))
H2D = [f for f in V["frames"] if f["dir"] == host.DIR_H2D]


class Dev:
    """Protocol と、それが送った行を記録するホスト側の相手。"""

    def __init__(self, key=KEY):
        self.sent = []
        self.p = bp.Protocol(key, self.sent.append)
        self.host = None

    def handshake(self, nh=NH):
        self.sent.clear()
        self.p.on_line(host.hello(host.ROLE_HOST, nh).rstrip(b"\n"))
        assert len(self.sent) == 1
        nd = host.parse_hello(self.sent.pop(), host.ROLE_DEVICE)
        self.host = host.Session(*host.hkdf(KEY, nh, nd), host.DIR_H2D)
        return nd

    def push(self, msg):
        self.p.on_line(self.host.seal(msg).rstrip(b"\n"))

    def replies(self):
        out = [self.host.open(line) for line in self.sent]
        self.sent.clear()
        return out


@pytest.fixture
def fixed_nd(monkeypatch):
    monkeypatch.setattr(bp, "urandom", lambda n: ND)


def test_hello_reply_matches_vector(fixed_nd):
    d = Dev()
    d.p.on_line(V["hello_host"].encode())
    assert [s.decode().rstrip("\n") for s in d.sent] == [V["hello_device"]]
    assert d.p.ready


def test_vector_frames_then_rejects(fixed_nd):
    d = Dev()
    d.p.on_line(V["hello_host"].encode())
    d.sent.clear()
    for f in H2D:
        d.p.on_line(f["line"].encode())
    assert d.p.sessions[0]["id"] == "0f3c9a1e"
    assert [q["req"] for q in d.p.queue] == ["r1"]
    rev = d.p.rev
    for case in V["reject_h2d"]:
        d.p.on_line(case["line"].encode())
    assert d.p.rev == rev and d.sent == [] and len(d.p.queue) == 1


def test_data_before_session_dropped(fixed_nd):
    d = Dev()
    d.p.on_line(H2D[1]["line"].encode())
    assert d.p.queue == [] and d.sent == []


def test_unpaired_ignores_everything(fixed_nd):
    d = Dev(key=None)
    assert not d.p.paired
    d.p.on_line(V["hello_host"].encode())
    d.p.on_line(H2D[1]["line"].encode())
    assert d.sent == [] and not d.p.ready and d.p.queue == []


def test_hello_from_device_role_ignored(fixed_nd):
    d = Dev()
    d.p.on_line(V["hello_device"].encode())
    assert d.sent == [] and not d.p.ready


def test_rehello_resets_session_and_queue():
    d = Dev()
    nd1 = d.handshake()
    d.push({"t": "perm", "n": 1, "req": "r1", "tool": "Bash", "hint": "ls"})
    old = d.host
    stale = old.seal({"t": "perm", "n": 1, "req": "r2", "tool": "Bash", "hint": "ls"}).rstrip(b"\n")
    assert len(d.p.queue) == 1
    nd2 = d.handshake(nh=bytes(16))
    assert nd1 != nd2
    assert d.p.queue == []
    d.p.on_line(stale)
    assert d.p.queue == []
    d.push({"t": "perm", "n": 1, "req": "r3", "tool": "Bash", "hint": "ls"})
    assert [q["req"] for q in d.p.queue] == ["r3"]


def test_disconnect_drops_session():
    d = Dev()
    d.handshake()
    d.push({"t": "perm", "n": 1, "req": "r1", "tool": "Bash", "hint": "ls"})
    d.p.on_disconnect()
    assert not d.p.ready and d.p.queue == []
    assert d.p.reply_perm("r1", "allow") is False


def test_dispatch_and_replies():
    d = Dev()
    d.handshake()
    d.push({"t": "sessions", "s": [{"n": 2, "id": "abcd1234", "name": "x", "title": "y", "state": "ask"}]})
    d.push({"t": "perm", "n": 2, "req": "p1", "tool": "Edit", "hint": "/a", "full": True})
    d.push({"t": "perm", "n": 2, "req": "p1", "tool": "Edit", "hint": "/a", "full": True})  # 再送は重複させない
    qs = [{"q": "which?", "h": "H", "o": ["a", "b"], "m": False}, {"q": "many?", "h": "M", "o": ["a", "b", "c"], "m": True}]
    d.push({"t": "ask", "n": 2, "req": "a1", "qs": qs})
    d.push({"t": "perm", "n": 2, "req": "p2", "tool": "Bash", "hint": "ls"})
    assert d.p.sessions[0]["n"] == 2
    assert [q["req"] for q in d.p.queue] == ["p1", "a1", "p2"]

    d.push({"t": "resolved", "req": "a1", "by": "terminal"})
    assert [q["req"] for q in d.p.queue] == ["p1", "p2"]

    assert d.p.reply_perm("p1", "allow")
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "allow"}]
    assert [q["req"] for q in d.p.queue] == ["p2"]

    d.push({"t": "ask", "n": 2, "req": "a2", "qs": qs})
    assert d.p.reply_ask("a2", [[1], [0, 2]])
    assert d.replies() == [{"t": "ask_reply", "req": "a2", "answers": [[1], [0, 2]]}]
    assert [q["req"] for q in d.p.queue] == ["p2"]

    d.push({"t": "ack_prompt", "n": 2, "ok": True, "queued": True})
    assert d.p.last_ack == {"t": "ack_prompt", "n": 2, "ok": True, "queued": True}


def test_allow_requires_full():
    d = Dev()
    d.handshake()
    d.push({"t": "perm", "n": 1, "req": "p1", "tool": "Bash", "hint": "ls"})
    d.push({"t": "perm", "n": 1, "req": "p2", "tool": "Bash", "hint": "ls", "full": False})
    d.push({"t": "perm", "n": 1, "req": "p3", "tool": "Bash", "hint": "ls", "full": "yes"})
    for req in ("p1", "p2", "p3"):
        assert d.p.reply_perm(req, "allow") is False
    assert d.sent == [] and len(d.p.queue) == 3
    assert d.p.reply_perm("p2", "deny")
    assert d.replies() == [{"t": "perm_reply", "req": "p2", "decision": "deny"}]


def test_queue_capped_at_16_dropping_oldest():
    d = Dev()
    d.handshake()
    for i in range(20):
        d.push({"t": "perm", "n": 1, "req": "r%d" % i, "tool": "Bash", "hint": "ls", "full": True})
    assert [q["req"] for q in d.p.queue] == ["r%d" % i for i in range(4, 20)]


def test_inbox_push_caps_total_bytes():
    inbox = []
    for i in range(10):
        bp.inbox_push(inbox, bytes([65 + i]) * 4000, 16384)
    assert sum(len(x) for x in inbox) <= 16384
    assert [x[0] for x in inbox] == [71, 72, 73, 74]  # 新しい 4 行が残る
    bp.inbox_push(inbox, b"x", 16384)
    assert inbox[-1] == b"x" and sum(len(x) for x in inbox) <= 16384


def test_malformed_messages_dropped():
    d = Dev()
    d.handshake()
    for msg in (
        {"t": "perm", "n": 1},
        {"t": "ask", "n": 1, "req": "a", "qs": "nope"},
        {"t": "ask", "n": 1, "req": "a", "qs": [{"q": "x", "o": ["only"]}]},
        {"t": "sessions", "s": 3},
        {"t": "log", "n": 1, "items": "nope"},
        {"t": "log", "n": 1, "items": [{"r": "u"}]},
        {"t": "log", "n": 1, "p": "0", "more": False, "items": []},
        {"t": "mystery"},
        {"no": "t"},
    ):
        d.push(msg)
    assert d.p.queue == [] and d.p.sessions == [] and d.p.log is None


def test_log_request_and_dispatch():
    d = Dev()
    assert d.p.request_log(1, "0f3c9a1e") is False
    d.handshake()
    assert d.p.request_log(1, "0f3c9a1e")
    assert d.p.request_log(1, "0f3c9a1e", 2)
    assert d.replies() == [{"t": "log_req", "n": 1, "id": "0f3c9a1e", "p": 0}, {"t": "log_req", "n": 1, "id": "0f3c9a1e", "p": 2}]
    rev = d.p.rev
    items = [{"r": "u", "x": "実装して"}, {"r": "a", "x": "はい\n終わりました", "c": True}]
    msg = {"t": "log", "n": 1, "p": 0, "more": True, "items": items}
    d.push(msg)
    assert d.p.log == msg and d.p.rev > rev
    assert d.p.take_log() == msg and d.p.log is None and d.p.take_log() is None
    d.push({"t": "log", "n": 2, "p": 1, "more": False, "items": []})
    assert d.p.log["n"] == 2 and d.p.log["items"] == []
    d.handshake(nh=bytes(16))
    assert d.p.log is None


def test_prompt_truncated_to_500():
    d = Dev()
    d.handshake()
    assert d.p.send_prompt(1, "0f3c9a1e", "a" * 600)
    (msg,) = d.replies()
    assert msg["n"] == 1 and msg["id"] == "0f3c9a1e"
    assert len(msg["text"]) == 500 and msg["text"].endswith("…")
    assert d.p.send_prompt(1, "0f3c9a1e", "short")
    assert d.replies()[0]["text"] == "short"


def test_reply_without_session_or_unknown_req():
    d = Dev()
    assert d.p.send_prompt(1, "x", "hi") is False
    d.handshake()
    assert d.p.reply_perm("nope", "allow") is False
    assert d.sent == []


def test_overlong_line_dropped(fixed_nd):
    d = Dev()
    d.p.on_line(V["hello_host"].encode())
    rev = d.p.rev
    d.p.on_line(b"A" * 4097)
    assert d.p.rev == rev and d.p.ready


def test_load_key(tmp_path):
    good = tmp_path / "k.bin"
    good.write_bytes(KEY)
    assert bp.load_key(str(good)) == KEY
    short = tmp_path / "s.bin"
    short.write_bytes(b"x" * 31)
    assert bp.load_key(str(short)) is None
    assert bp.load_key(str(tmp_path / "missing.bin")) is None
