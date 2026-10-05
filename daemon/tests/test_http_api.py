import asyncio
import contextlib
import functools
import json
import os
import shutil
import stat
import tempfile
import time

from cardbuddy import http_api
from cardbuddy.buddyd import Hub

SID = "0f3c9a1e-aaaa-bbbb-cccc-000000000001"
QS = [{"q": "どれ？", "h": "Approach", "o": ["A", "B"], "m": False}]


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


class FakeLink:
    connected = True
    device = "Claude_ab12cd"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)

    def types(self):
        return [m["t"] for m in self.sent]


@contextlib.asynccontextmanager
async def daemon():
    # macOS の sun_path は 104 byte までなので pytest の tmp_path は使わない
    d = tempfile.mkdtemp(prefix="cb")
    path = os.path.join(d, "h", "s.sock")
    hub = Hub(FakeLink())
    server = await http_api.serve(hub, path)
    task = asyncio.create_task(hub.sessions_loop())
    try:
        yield hub, path
    finally:
        task.cancel()
        server.close()
        shutil.rmtree(d)


async def call(path, method, target, body=None, raw=None, headers=""):
    r, w = await asyncio.open_unix_connection(path)
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    w.write(f"{method} {target} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n{headers}\r\n".encode() + data)
    await w.drain()
    resp = await r.read()
    w.close()
    head, _, payload = resp.partition(b"\r\n\r\n")
    assert b"Connection: close" in head
    return int(head.split()[1]), json.loads(payload)


async def register(path, sid=SID, state="idle"):
    return await call(path, "POST", "/session", {"sid": sid, "cwd": "/w/proj", "title": "やって", "state": state})


@aio
async def test_socket_and_dir_permissions():
    async with daemon() as (_, path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode) == 0o700


@aio
async def test_second_daemon_refuses_live_socket_and_replaces_stale_one():
    async with daemon() as (hub, path):
        try:
            await http_api.serve(hub, path)
        except SystemExit:
            pass
        else:
            raise AssertionError("expected SystemExit")
    d = tempfile.mkdtemp(prefix="cb")
    stale = os.path.join(d, "s.sock")
    open(stale, "w").close()
    server = await http_api.serve(Hub(FakeLink()), stale)
    server.close()
    shutil.rmtree(d)


@aio
async def test_session_lifecycle_and_status():
    async with daemon() as (hub, path):
        assert await register(path) == (200, {"n": 1})
        assert await register(path, "other-sid", "running") == (200, {"n": 2})
        assert await call(path, "GET", "/status") == (200, {
            "connected": True, "device": "Claude_ab12cd",
            "sessions": [{"n": 1, "sid": SID, "state": "idle"}, {"n": 2, "sid": "other-sid", "state": "running"}]})
        assert await call(path, "DELETE", "/session/other-sid") == (200, {})
        assert [s["sid"] for s in (await call(path, "GET", "/status"))[1]["sessions"]] == [SID]


@aio
async def test_sessions_are_debounced_and_deduped():
    async with daemon() as (hub, path):
        await register(path)
        await register(path, "other-sid")
        assert hub.link.types() == []
        await asyncio.sleep(0.7)
        assert hub.link.sent == [hub.table.sessions_msg()]
        assert len(hub.link.sent[0]["s"]) == 2
        await register(path)
        await asyncio.sleep(0.7)
        assert hub.link.types() == ["sessions"]


CMD = {"command": "rm -rf ./build"}


@aio
async def test_perm_wait_gets_device_decision():
    async with daemon() as (hub, path):
        await register(path)
        code, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        req = body["req"]
        assert code == 200 and hub.link.sent[-1] == {"t": "perm", "n": 1, "req": req, "id": SID[:8], "name": "proj",
                                                       "tool": "Bash", "desc": "", "hint": "rm -rf ./build", "full": True}
        wait = asyncio.create_task(call(path, "GET", f"/perm/{req}?timeout=5"))
        poll = asyncio.create_task(call(path, "GET", f"/poll?sid={SID}&timeout=0.5"))
        await asyncio.sleep(0.1)
        assert not wait.done()
        t0 = time.monotonic()
        hub.on_msg({"t": "perm_reply", "req": req, "decision": "allow"})
        assert await wait == (200, {"decision": "allow"})
        assert time.monotonic() - t0 < 1
        assert hub.link.sent[-1] == {"t": "resolved", "req": req, "by": "device"}
        assert await poll == (200, {"events": []})
        assert await call(path, "GET", f"/perm/{req}?timeout=5") == (200, {"decision": "allow"})


@aio
async def test_perm_wait_timeout_and_unknown():
    async with daemon() as (hub, path):
        await register(path)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        t0 = time.monotonic()
        assert await call(path, "GET", f"/perm/{body['req']}?timeout=0.3") == (200, {})
        assert 0.25 < time.monotonic() - t0 < 2
        assert (await call(path, "GET", "/perm/rnope?timeout=0.1"))[0] == 404
        _, ask = await call(path, "POST", "/ask", {"sid": SID, "qs": QS})
        assert (await call(path, "GET", f"/perm/{ask['req']}?timeout=0.1"))[0] == 404
        assert (await call(path, "GET", f"/perm/{body['req']}?timeout=x"))[0] == 400


@aio
async def test_tool_done_releases_perm_wait():
    async with daemon() as (hub, path):
        await register(path)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": {"command": "ls", "timeout": 5}})
        wait = asyncio.create_task(call(path, "GET", f"/perm/{body['req']}?timeout=5"))
        await asyncio.sleep(0.1)
        assert await call(path, "POST", "/tool_done", {"sid": SID, "tool": "Bash", "input": {"command": "ls"}}) == (200, {})
        assert not wait.done()
        assert await call(path, "POST", "/tool_done", {"sid": SID, "tool": "Bash", "input": {"timeout": 5, "command": "ls"}}) == (200, {})
        assert await asyncio.wait_for(wait, 2) == (200, {"released": True})
        assert hub.link.sent[-1] == {"t": "resolved", "req": body["req"], "by": "terminal"}
        assert await call(path, "POST", "/tool_done", {"sid": "gone", "tool": "Bash", "input": {}}) == (200, {})


@aio
async def test_perm_wait_released_when_session_goes_away():
    async with daemon() as (hub, path):
        await register(path)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        wait = asyncio.create_task(call(path, "GET", f"/perm/{body['req']}?timeout=5"))
        await asyncio.sleep(0.1)
        await call(path, "DELETE", f"/session/{SID}")
        assert await asyncio.wait_for(wait, 2) == (200, {"released": True})


@aio
async def test_perm_wait_released_on_ttl(monkeypatch):
    from cardbuddy import buddyd
    monkeypatch.setattr(buddyd, "EXPIRE_INTERVAL", 0.05)
    async with daemon() as (hub, path):
        await register(path)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        hub.table.sessions[SID]["seen"] -= 100
        task = asyncio.create_task(hub.expire_loop())
        try:
            assert await call(path, "GET", f"/perm/{body['req']}?timeout=5") == (200, {"released": True})
        finally:
            task.cancel()


@aio
async def test_sessions_resent_with_derived_state():
    async with daemon() as (hub, path):
        await register(path, state="running")
        await asyncio.sleep(0.7)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        await asyncio.sleep(0.7)
        assert hub.link.types() == ["sessions", "perm", "sessions"]
        assert hub.link.sent[-1]["s"][0]["state"] == "perm"
        assert (await call(path, "GET", "/status"))[1]["sessions"][0]["state"] == "perm"
        await call(path, "POST", "/resolved", {"req": body["req"], "by": "abort"})
        await asyncio.sleep(0.7)
        assert hub.link.types()[-2:] == ["resolved", "sessions"]
        assert hub.link.sent[-1]["s"][0]["state"] == "running"


def test_wait_timeouts_are_capped_at_25():
    assert http_api.WAIT_DEFAULT == http_api.WAIT_MAX == 25


@aio
async def test_poll_returns_queued_events_immediately_and_times_out_empty():
    async with daemon() as (hub, path):
        await register(path)
        hub.on_msg({"t": "prompt", "n": 1, "id": SID[:8], "text": "hello"})
        assert await call(path, "GET", f"/poll?sid={SID}") == (200, {"events": [{"type": "prompt", "text": "hello"}]})
        t0 = time.monotonic()
        assert await call(path, "GET", f"/poll?sid={SID}&timeout=0.3") == (200, {"events": []})
        assert 0.25 < time.monotonic() - t0 < 2
        assert (await call(path, "GET", f"/poll?sid={SID}&timeout=abc"))[0] == 400
        assert (await call(path, "GET", f"/poll?sid={SID}&timeout=nan"))[0] == 400
        assert (await call(path, "GET", "/poll"))[0] == 400


@aio
async def test_newer_poll_preempts_older_one():
    async with daemon() as (hub, path):
        await register(path)
        old = asyncio.create_task(call(path, "GET", f"/poll?sid={SID}&timeout=10"))
        await asyncio.sleep(0.1)
        new = asyncio.create_task(call(path, "GET", f"/poll?sid={SID}&timeout=10"))
        assert await asyncio.wait_for(old, 2) == (200, {"events": []})
        hub.on_msg({"t": "prompt", "n": 1, "id": SID[:8], "text": "x"})
        assert await asyncio.wait_for(new, 2) == (200, {"events": [{"type": "prompt", "text": "x"}]})


@aio
async def test_ask_resolved_and_ack_prompt():
    async with daemon() as (hub, path):
        await register(path)
        code, body = await call(path, "POST", "/ask", {"sid": SID, "qs": QS})
        assert code == 200 and hub.link.sent[-1]["t"] == "ask"
        assert await call(path, "POST", "/resolved", {"req": body["req"], "by": "terminal"}) == (200, {})
        assert hub.link.sent[-1] == {"t": "resolved", "req": body["req"], "by": "terminal"}
        assert await call(path, "POST", "/resolved", {"req": body["req"], "by": "terminal"}) == (200, {})
        assert await call(path, "POST", "/ack_prompt", {"sid": SID, "queued": True}) == (200, {})
        assert hub.link.sent[-1] == {"t": "ack_prompt", "n": 1, "ok": True, "queued": True}


@aio
async def test_validation_errors():
    async with daemon() as (hub, path):
        await register(path)
        bad = [
            ("POST", "/session", {"sid": SID, "cwd": "/x", "title": "", "state": "sleeping"}, 400),
            ("POST", "/session", {"sid": "", "cwd": "/x", "title": "", "state": "idle"}, 400),
            ("POST", "/session", ["not", "object"], 400),
            ("POST", "/session", {"sid": SID, "cwd": "/x", "title": "", "state": "perm"}, 400),
            ("POST", "/perm", {"sid": "unknown", "tool": "Bash", "input": {}}, 404),
            ("POST", "/perm", {"sid": SID, "tool": 1, "input": {}}, 400),
            ("POST", "/perm", {"sid": SID, "tool": "Bash", "input": "ls"}, 400),
            ("POST", "/perm", {"sid": SID, "tool": "Bash", "desc": "", "hint": "ls"}, 400),
            ("POST", "/tool_done", {"sid": SID, "tool": "Bash", "input": []}, 400),
            ("POST", "/ask", {"sid": SID, "qs": []}, 400),
            ("POST", "/ask", {"sid": SID, "qs": [{"q": "a", "h": "b", "o": ["only"], "m": False}]}, 400),
            ("POST", "/ask", {"sid": SID, "qs": [{"q": "a", "h": "b", "o": ["x", "y"], "m": 0}]}, 400),
            ("POST", "/ask", {"sid": SID, "qs": QS * 5}, 400),
            ("POST", "/resolved", {"req": "r1", "by": "device"}, 400),
            ("POST", "/ack_prompt", {"sid": SID, "queued": "yes"}, 400),
            ("POST", "/ack_prompt", {"sid": "unknown", "queued": True}, 404),
            ("GET", "/nope", None, 404),
            ("PUT", "/session", None, 404),
        ]
        for method, target, body, want in bad:
            code, resp = await call(path, method, target, body)
            assert (code, "error" in resp) == (want, True), (target, body)
        assert (await call(path, "POST", "/session", raw=b"{nope"))[0] == 400
        r, w = await asyncio.open_unix_connection(path)
        w.write(f"POST /session HTTP/1.1\r\nContent-Length: {http_api.MAX_BODY + 1}\r\n\r\n".encode())
        await w.drain()
        assert (await r.read()).startswith(b"HTTP/1.1 413")
        w.close()
        code, _ = await call(path, "POST", "/session", raw=b"", headers="Transfer-Encoding: chunked\r\n")
        assert code == 411
        assert hub.link.types() == []


@aio
async def test_garbage_request_does_not_kill_server():
    async with daemon() as (_, path):
        r, w = await asyncio.open_unix_connection(path)
        w.write(b"\x00\x01garbage\r\n\r\n")
        await w.drain()
        assert (await r.read()).startswith(b"HTTP/1.1 400")
        w.close()
        assert (await call(path, "GET", "/status"))[0] == 200


@aio
async def test_lone_surrogates():
    async with daemon() as (hub, path):
        code, _ = await call(path, "POST", "/session", raw=b'{"sid":"s","cwd":"/x","title":"\\ud83d","state":"idle"}')
        assert code == 400
        await register(path)
        code, _ = await call(path, "POST", "/ask", raw=(
            b'{"sid":"%s","qs":[{"q":"a","h":"b","o":["\\ud83d","y"],"m":false}]}' % SID.encode()))
        assert code == 400
        hub.on_msg({"t": "prompt", "n": 1, "id": SID[:8], "text": "\ud83d"})
        assert (await call(path, "GET", f"/poll?sid={SID}"))[1] == {"events": [{"type": "prompt", "text": "\ud83d"}]}


@aio
async def test_requests_are_sent_once_session_gets_a_number():
    async with daemon() as (hub, path):
        for i in range(9):
            await register(path, f"sid-{i}")
        assert await register(path) == (200, {"n": None})
        _, body = await call(path, "POST", "/ask", {"sid": SID, "qs": QS})
        assert "ask" not in hub.link.types()
        await call(path, "DELETE", "/session/sid-3")
        assert await register(path) == (200, {"n": 4})
        assert hub.link.sent[-1] == {"t": "ask", "n": 4, "req": body["req"], "id": SID[:8], "name": "proj", "qs": QS}
        assert hub.table.pending_msgs() == [hub.link.sent[-1]]


@aio
async def test_post_log():
    async with daemon() as (hub, path):
        assert await call(path, "POST", "/log", {"sid": "unknown", "role": "user", "text": "x"}) == (200, {})
        await register(path)
        assert await call(path, "POST", "/log", {"sid": SID, "role": "user", "text": "質問"}) == (200, {})
        assert await call(path, "POST", "/log", {"sid": SID, "role": "assistant", "text": "答え"}) == (200, {})
        for bad in ({"sid": SID, "role": "system", "text": "x"}, {"sid": SID, "role": "user", "text": 1}):
            assert (await call(path, "POST", "/log", bad))[0] == 400
        hub.on_msg({"t": "log_req", "n": 1, "id": SID[:8], "p": 0})
        assert hub.link.sent[-1] == {"t": "log", "n": 1, "p": 0, "more": False, "items": [
            {"r": "u", "x": "質問", "c": False}, {"r": "a", "x": "答え", "c": False}]}


@aio
async def test_assistant_log_resends_sessions():
    async with daemon() as (hub, path):
        await register(path)
        await asyncio.sleep(0.7)
        assert hub.link.sent[-1]["s"][0]["last"] == ""
        await call(path, "POST", "/log", {"sid": SID, "role": "assistant", "text": "できました\n詳細"})
        await asyncio.sleep(0.7)
        assert hub.link.types() == ["sessions", "sessions"]
        assert hub.link.sent[-1]["s"][0]["last"] == "できました"


@aio
async def test_perm_wait_tracks_idle_time():
    async with daemon() as (hub, path):
        await register(path)
        _, body = await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD})
        req = body["req"]
        r = hub.table.reqs[req]
        wait = asyncio.create_task(call(path, "GET", f"/perm/{req}?timeout=0.3"))
        await asyncio.sleep(0.1)
        assert r["waiters"] == 1
        far = time.monotonic() + 1000
        hub.table.sessions[SID]["seen"] = far
        assert hub.table.expire(far) == []
        assert await wait == (200, {})
        assert r["waiters"] == 0 and time.monotonic() - r["idle_since"] < 0.5
        assert hub.table.expire(far) == [{"t": "resolved", "req": req, "by": "abort"}]
        assert await call(path, "GET", f"/perm/{req}?timeout=1") == (200, {"released": True})



@aio
async def test_perm_unavailable_when_disconnected_or_unnumbered():
    async with daemon() as (hub, path):
        await register(path)
        hub.link.connected = False
        assert (await call(path, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": CMD}))[0] == 503
        hub.link.connected = True
        for i in range(9):
            await register(path, f"sid-{i}")
        await register(path, "extra")
        assert (await call(path, "POST", "/perm", {"sid": "extra", "tool": "Bash", "input": CMD}))[0] == 503
        assert hub.table.reqs == {}


def test_on_up_resends_newest_8_requests():
    hub = Hub(FakeLink())
    hub.table.upsert(SID, "/w", "", "idle", 0)
    reqs = [hub.table.add_perm(SID, "Bash", {"command": str(i)}, 0)[0] for i in range(10)]
    hub.on_up()
    assert hub.link.types() == ["sessions"] + ["perm"] * 8
    assert [m["req"] for m in hub.link.sent[1:]] == reqs[2:]
