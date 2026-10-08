import asyncio
import functools

from cardbuddy.buddyd import Hub
from cardbuddy.session_table import SessionTable

SID = "0f3c9a1e-aaaa-bbbb-cccc-000000000001"
QS = [{"q": "質" * 200, "h": "見出し", "o": ["選" * 60, "B"], "m": True}]


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


class FakeLink:
    transport = "ble"
    device = "Claude_ab12cd"

    def __init__(self, connected=True):
        self.connected = connected
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


def test_web_views_keep_full_text():
    t = SessionTable()
    t.upsert(SID, "/w/proj", "最初のプロンプト" + "x" * 100 + "\n2行目", "running", 0)
    t.add_log(SID, "user", "質問")
    t.add_log(SID, "assistant", "返答の1行目" + "y" * 300 + "\n続き")
    cmd = "echo " + "あ" * 1500
    req, msgs = t.add_perm(SID, "Bash", {"command": cmd, "description": "d" * 100}, 0)
    assert msgs[0]["full"] is False
    assert t.web_req(req) == {"t": "perm", "req": req, "sid": SID, "n": 1, "id": SID[:8], "name": "proj",
                              "tool": "Bash", "desc": "d" * 100, "hint": cmd, "extra": ""}
    areq, _ = t.add_ask(SID, QS)
    assert t.web_req(areq) == {"t": "ask", "req": areq, "sid": SID, "n": 1, "id": SID[:8], "name": "proj", "qs": QS}
    assert [r["req"] for r in t.web_reqs()] == [req, areq]
    s, = t.web_sessions()
    assert s == {"sid": SID, "n": 1, "id": SID[:8], "name": "proj", "title": ("最初のプロンプト" + "x" * 100)[:79] + "…",
                 "state": "ask", "last": ("返答の1行目" + "y" * 300)[:199] + "…", "updated_at": 0}
    assert t.web_log(SID) == [{"r": "u", "x": "質問"}, {"r": "a", "x": "返答の1行目" + "y" * 300 + "\n続き"}]
    assert t.web_log("unknown") is None
    assert t.web_req("unknown") is None


def test_web_sessions_include_unnumbered():
    t = SessionTable()
    for i in range(10):
        t.upsert(f"s{i}", "/w", "", "idle", 0)
    rows = t.web_sessions()
    assert len(rows) == 10 and rows[-1]["n"] is None and rows[-1]["sid"] == "s9"


@aio
async def test_hub_publishes_to_web_subscribers():
    hub = Hub(FakeLink())
    hub.table.upsert(SID, "/w/proj", "", "running", 0)
    q = hub.web_subscribe()
    snap = hub.web_snapshot()
    assert snap[0]["t"] == "sessions" and snap[0]["s"][0]["sid"] == SID
    req, msgs = hub.table.add_perm(SID, "Bash", {"command": "ls"}, 0)
    hub.send(msgs)
    hub.web_new_req(req)
    assert q.get_nowait()["t"] == "perm" and q.empty()
    hub.on_msg({"t": "perm_reply", "req": req, "decision": "allow"})
    assert q.get_nowait() == {"t": "resolved", "req": req, "by": "device"}
    assert hub.link.sent[-1] == {"t": "resolved", "req": req, "by": "device"}
    hub.send([hub.table.sessions_msg()])
    ev = q.get_nowait()
    assert ev["t"] == "sessions" and "sid" in ev["s"][0]
    hub.send([{"t": "log", "n": 1, "p": 0, "more": False, "items": []}])
    assert q.empty()
    hub.web_unsubscribe(q)
    hub.web_new_req(req)
    assert q.empty()


@aio
async def test_slow_web_subscriber_is_dropped():
    hub = Hub(FakeLink())
    hub.table.upsert(SID, "/w", "", "running", 0)
    q = hub.web_subscribe()
    for _ in range(q.maxsize + 5):
        hub.send([{"t": "resolved", "req": "r1", "by": "abort"}])
    assert q not in hub.web_subs


def test_web_perm_shows_inputs_not_in_hint():
    t = SessionTable()
    t.upsert(SID, "/w/proj", "", "running", 0)
    req, _ = t.add_perm(SID, "Bash", {"command": "ls", "description": "d", "run_in_background": True}, 0)
    assert t.web_req(req)["extra"] == '{"run_in_background":true}'
    req, _ = t.add_perm(SID, "Edit", {"file_path": "/a", "old_string": "x", "new_string": "y", "replace_all": False}, 0)
    assert t.web_req(req)["extra"] == ""
    req, _ = t.add_perm(SID, "WebFetch", {"url": "u"}, 0)
    assert t.web_req(req)["extra"] == ""


def test_updated_at_is_web_only():
    t = SessionTable()
    t.upsert(SID, "/w", "", "running", 0)
    assert t.web_sessions()[0]["updated_at"] == 0
    t.touch(SID, 1234.5)
    t.touch("unknown", 99)
    assert t.web_sessions()[0]["updated_at"] == 1234.5
    assert "updated_at" not in t.sessions_msg()["s"][0]


def test_web_perm_carries_edit_and_write_fields():
    t = SessionTable()
    t.upsert(SID, "/w/proj", "", "running", 0)
    req, _ = t.add_perm(SID, "Edit", {"file_path": "/a.py", "old_string": "a\n- b", "new_string": "c",
                                      "replace_all": True}, 0)
    assert t.web_req(req)["diff"] == {"path": "/a.py", "old": "a\n- b", "new": "c", "replace_all": True}
    req, _ = t.add_perm(SID, "Write", {"file_path": "/b.txt", "content": "x\ny"}, 0)
    assert t.web_req(req)["diff"] == {"path": "/b.txt", "content": "x\ny"}
    req, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    assert "diff" not in t.web_req(req)
