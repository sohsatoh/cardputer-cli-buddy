import asyncio
import contextlib
import functools
import json
import shutil
import ssl
import tempfile
from pathlib import Path

import pytest

from cardbuddy import http_api, web, webpki
from cardbuddy.buddyd import Hub

SID = "0f3c9a1e-aaaa-bbbb-cccc-000000000001"
UI = Path(web.__file__).parent / "webui"


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


class FakeLink:
    connected = False
    transport = None
    device = None

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cb"))
    monkeypatch.setenv("CARDBUDDY_HOME", str(d / "h"))
    monkeypatch.setattr(webpki, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(webpki, "local_hostname", lambda: "macbook")
    webpki.init()
    webpki.enroll_pem("mac")
    yield d / "h"
    shutil.rmtree(d)


@contextlib.asynccontextmanager
async def running(home):
    hub = Hub(FakeLink())
    srv = await web.serve_web(hub, "127.0.0.1", 0)
    sock = str(home / "s.sock")
    api = await http_api.serve(hub, sock)
    try:
        yield hub, srv.sockets[0].getsockname()[1], sock
    finally:
        srv.close()
        api.close()


def ctx(home, name="mac", ca_dir=None):
    pki = home / "pki"
    c = ssl.create_default_context(cafile=str((ca_dir or pki) / "ca.crt"))
    if name:
        c.load_cert_chain(str(pki / f"client-{name}.crt"), str(pki / f"client-{name}.key"))
    return c


async def open_(port, c):
    return await asyncio.open_connection("127.0.0.1", port, ssl=c, server_hostname="localhost")


async def request(port, c, method, path, body=None, headers=None, csrf=True):
    r, w = await open_(port, c)
    data = json.dumps(body).encode() if body is not None else b""
    h = {"Host": f"localhost:{port}", "Content-Length": str(len(data))}
    if method == "POST" and csrf:
        h.update({"Content-Type": "application/json", "X-CardBuddy": "1", "Origin": f"https://localhost:{port}"})
    h.update(headers or {})
    head = "".join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
    w.write(f"{method} {path} HTTP/1.1\r\n{head}\r\n".encode() + data)
    await w.drain()
    raw = await asyncio.wait_for(r.read(), 5)
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    lines = head.decode().split("\r\n")
    hdrs = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
    return int(lines[0].split()[1]), hdrs, payload


async def unix(sock, method, target, body=None):
    r, w = await asyncio.open_unix_connection(sock)
    data = json.dumps(body).encode() if body is not None else b""
    w.write(f"{method} {target} HTTP/1.1\r\nHost: x\r\nContent-Length: {len(data)}\r\n\r\n".encode() + data)
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(payload)


async def sse_events(r, n):
    out = []
    while len(out) < n:
        line = await asyncio.wait_for(r.readline(), 5)
        assert line, "stream closed"
        if line.startswith(b"data: "):
            out.append(json.loads(line[6:]))
    return out


async def open_sse(port, c):
    r, w = await open_(port, c)
    w.write(f"GET /api/events HTTP/1.1\r\nHost: localhost:{port}\r\n\r\n".encode())
    await w.drain()
    head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 5)
    assert head.startswith(b"HTTP/1.1 200") and b"text/event-stream" in head
    return r, w


@aio
async def test_mtls_requires_enrolled_certificate(home, monkeypatch):
    other = Path(tempfile.mkdtemp(prefix="cb"))
    async with running(home) as (hub, port, _):
        status, hdrs, body = await request(port, ctx(home), "GET", "/")
        assert status == 200 and b"/app.js" in body
        assert hdrs["content-security-policy"] == "default-src 'self'"
        assert hdrs["x-frame-options"] == "DENY" and hdrs["cache-control"] == "no-store"
        assert hdrs["x-content-type-options"] == "nosniff"
        with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError)):
            await request(port, ctx(home, name=None), "GET", "/")
        webpki.enroll_pem("ghost")
        webpki.revoke("ghost")
        assert (await request(port, ctx(home, "ghost"), "GET", "/"))[0] == 403
        assert webpki.revoke("mac") == 1
        assert (await request(port, ctx(home), "GET", "/"))[0] == 403
    monkeypatch.setenv("CARDBUDDY_HOME", str(other / "h"))
    webpki.init()
    webpki.enroll_pem("mac")
    foreign = ssl.create_default_context(cafile=str(home / "pki" / "ca.crt"))
    foreign.load_cert_chain(str(other / "h/pki/client-mac.crt"), str(other / "h/pki/client-mac.key"))
    monkeypatch.setenv("CARDBUDDY_HOME", str(home))
    async with running(home) as (hub, port, _):
        with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError)):
            await request(port, foreign, "GET", "/")
    shutil.rmtree(other)


@aio
async def test_static_files_and_unknown_paths(home):
    async with running(home) as (hub, port, _):
        st, h, body = await request(port, ctx(home), "GET", "/app.js")
        assert st == 200 and h["content-type"].startswith("text/javascript") and body == (UI / "app.js").read_bytes()
        st, h, _ = await request(port, ctx(home), "GET", "/app.css")
        assert st == 200 and h["content-type"].startswith("text/css")
        assert (await request(port, ctx(home), "GET", "/../buddyd.py"))[0] == 404
        assert (await request(port, ctx(home), "GET", "/api/perm_reply"))[0] == 405


@aio
async def test_csrf_guards(home):
    async with running(home) as (hub, port, _):
        body = {"req": "rnope", "decision": "allow"}
        c = ctx(home)
        assert (await request(port, c, "POST", "/api/perm_reply", body))[0] == 200
        assert (await request(port, c, "POST", "/api/perm_reply", body, {"X-CardBuddy": None}))[0] == 403
        assert (await request(port, c, "POST", "/api/perm_reply", body, {"Origin": "https://evil.example"}))[0] == 403
        assert (await request(port, c, "POST", "/api/perm_reply", body, {"Origin": None}))[0] == 403
        assert (await request(port, c, "POST", "/api/perm_reply", body,
                              {"Origin": f"http://localhost:{port}"}))[0] == 403
        assert (await request(port, c, "POST", "/api/perm_reply", body, {"Content-Type": "text/plain"}))[0] == 403
        st, h, _ = await request(port, c, "OPTIONS", "/api/perm_reply", headers={
            "Origin": "https://evil.example", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-cardbuddy"})
        assert st == 403 and not any(k.startswith("access-control-") for k in h)


@aio
async def test_web_allow_reaches_perm_hook_wait(home):
    async with running(home) as (hub, port, sock):
        c = ctx(home)
        r, w = await open_sse(port, c)
        first = (await sse_events(r, 1))[0]
        assert (first["t"], first["s"]) == ("sessions", [])
        assert await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w/proj", "title": "t", "state": "running"}) == (
            200, {"n": 1})
        cmd = "rm -rf ./build <script>alert(1)</script> " + "あ" * 1500
        st, body = await unix(sock, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": {"command": cmd}})
        assert st == 200
        req = body["req"]
        ev = (await sse_events(r, 1))[0]
        assert ev == {"t": "perm", "req": req, "sid": SID, "n": 1, "id": SID[:8], "name": "proj",
                      "tool": "Bash", "desc": "", "hint": cmd, "extra": ""}
        wait = asyncio.create_task(unix(sock, "GET", f"/perm/{req}?timeout=5"))
        await asyncio.sleep(0.1)
        st, _, resp = await request(port, c, "POST", "/api/perm_reply", {"req": req, "decision": "allow"})
        assert (st, json.loads(resp)) == (200, {"ok": True})
        assert await wait == (200, {"decision": "allow"})
        assert (await sse_events(r, 1))[0] == {"t": "resolved", "req": req, "by": "web"}
        st, _, resp = await request(port, c, "POST", "/api/perm_reply", {"req": req, "decision": "deny"})
        assert json.loads(resp) == {"ok": False}
        w.close()


@aio
async def test_sse_snapshot_includes_pending_requests(home):
    async with running(home) as (hub, port, sock):
        hub.table.upsert(SID, "/w/proj", "t", "running", 0)
        req, _ = hub.table.add_ask(SID, [{"q": "どれ？", "h": "H", "o": ["A", "B"], "m": False}])
        r, w = await open_sse(port, ctx(home))
        sessions, ask = await sse_events(r, 2)
        assert sessions["s"][0]["sid"] == SID and ask["t"] == "ask" and ask["req"] == req
        st, _, resp = await request(port, ctx(home), "POST", "/api/ask_reply", {"req": req, "answers": [[1]]})
        assert json.loads(resp) == {"ok": True}
        assert hub.table.pop_events(SID) == [{"type": "ask_reply", "req": req, "answers": [[1]]}]
        assert (await request(port, ctx(home), "POST", "/api/ask_reply", {"req": req, "answers": "x"}))[0] == 400
        w.close()


@aio
async def test_sse_closes_after_revoke(home, monkeypatch):
    monkeypatch.setattr(web, "HEARTBEAT", 0.1)
    async with running(home) as (hub, port, _):
        r, w = await open_sse(port, ctx(home))
        await sse_events(r, 1)
        while b": keepalive" not in await asyncio.wait_for(r.readline(), 2):
            pass
        webpki.revoke("mac")
        await asyncio.wait_for(r.read(), 2)
        assert not hub.web_subs


@aio
async def test_prompt_and_log_api(home):
    async with running(home) as (hub, port, _):
        hub.table.upsert(SID, "/w/proj", "t", "running", 0)
        hub.table.add_log(SID, "user", "質問")
        hub.table.add_log(SID, "assistant", "答え\n2行目")
        c = ctx(home)
        st, _, body = await request(port, c, "GET", f"/api/sessions/{SID}/log")
        assert (st, json.loads(body)) == (200, {"items": [{"r": "u", "x": "質問"}, {"r": "a", "x": "答え\n2行目"}]})
        assert (await request(port, c, "GET", "/api/sessions/nope/log"))[0] == 404
        st, _, body = await request(port, c, "POST", "/api/prompt", {"n": 1, "id": SID[:8], "text": "やって"})
        assert (st, json.loads(body)) == (200, {"ok": True})
        assert hub.table.pop_events(SID) == [{"type": "prompt", "text": "やって"}]
        assert (await request(port, c, "POST", "/api/prompt", {"n": 2, "id": SID[:8], "text": "x"}))[0] == 404
        assert (await request(port, c, "POST", "/api/prompt", {"n": 1, "id": SID[:8], "text": "x" * 501}))[0] == 400
        assert (await request(port, c, "POST", "/api/prompt", {"n": 1, "id": SID[:8], "text": ""}))[0] == 400
        assert hub.link.sent == []


@aio
async def test_perm_is_503_only_when_web_ui_is_disabled_and_no_device(home, caplog):
    caplog.set_level("INFO", logger="cardbuddy.http_api")
    hub = Hub(FakeLink())
    sock = str(home / "only-api.sock")
    api = await http_api.serve(hub, sock)
    await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w", "title": "t", "state": "running"})
    assert (await unix(sock, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": {}}))[0] == 503
    assert "api: POST /perm 503" in caplog.text
    api.close()


@aio
async def test_perm_and_ask_are_kept_for_web_without_subscribers(home):
    async with running(home) as (hub, port, sock):
        for i in range(9):
            await unix(sock, "POST", "/session", {"sid": f"s{i}", "cwd": "/w", "title": "t", "state": "running"})
        await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w/proj", "title": "t", "state": "running"})
        assert hub.table.sessions[SID]["n"] is None and not hub.web_subs and not hub.link.connected
        st, body = await unix(sock, "POST", "/perm", {"sid": SID, "tool": "Bash", "input": {"command": "gh pr merge 1"}})
        assert st == 200
        st, ask = await unix(sock, "POST", "/ask", {"sid": SID, "qs": [{"q": "q", "h": "h", "o": ["a", "b"], "m": False}]})
        r, w = await open_sse(port, ctx(home))
        events = await sse_events(r, 3)
        reqs = {e["req"]: e for e in events if e["t"] in ("perm", "ask")}
        assert reqs[body["req"]]["n"] is None and reqs[body["req"]]["sid"] == SID and ask["req"] in reqs
        wait = asyncio.create_task(unix(sock, "GET", f"/perm/{body['req']}?timeout=5"))
        await asyncio.sleep(0.05)
        await request(port, ctx(home), "POST", "/api/perm_reply", {"req": body["req"], "decision": "allow"})
        assert await wait == (200, {"decision": "allow"})
        st, _, resp = await request(port, ctx(home), "POST", "/api/prompt", {"sid": SID, "text": "番号なし"})
        assert (st, json.loads(resp)) == (200, {"ok": True})
        assert hub.table.pop_events(SID) == [{"type": "prompt", "text": "番号なし"}]
        assert (await request(port, ctx(home), "POST", "/api/prompt", {"sid": "nope", "text": "x"}))[0] == 404
        w.close()


def test_ui_never_uses_inner_html():
    js = (UI / "app.js").read_text()
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js
    html = (UI / "index.html").read_text()
    assert "<script>" not in html and "<style>" not in html and 'src="/app.js"' in html


@aio
async def test_host_must_be_a_server_certificate_name(home):
    async with running(home) as (hub, port, _):
        c = ctx(home)
        assert (await request(port, c, "GET", "/", headers={"Host": f"evil.example:{port}"}))[0] == 403
        st = (await request(port, c, "POST", "/api/perm_reply", {"req": "r", "decision": "deny"},
                            {"Host": f"evil.example:{port}", "Origin": f"https://evil.example:{port}"}))[0]
        assert st == 403
        assert (await request(port, c, "GET", "/", headers={"Host": f"127.0.0.1:{port}"}))[0] == 200


@aio
async def test_connection_cap_and_handshake_timeout(home, monkeypatch):
    monkeypatch.setattr(web, "MAX_CONN", 2)
    monkeypatch.setattr(web, "HANDSHAKE_TIMEOUT", 0.3)
    async with running(home) as (hub, port, _):
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
        await asyncio.sleep(0.05)
        with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError)):
            await request(port, ctx(home), "GET", "/")
        for r, w in idle:
            assert await asyncio.wait_for(r.read(), 2) == b""
        assert (await request(port, ctx(home), "GET", "/"))[0] == 200


@aio
async def test_revoke_during_request_read_is_honored(home):
    async with running(home) as (hub, port, _):
        r, w = await open_(port, ctx(home))
        w.write(b"GET / HTTP/1.1\r\nHost: localhost:" + str(port).encode() + b"\r\n")
        await w.drain()
        await asyncio.sleep(0.05)
        webpki.revoke("mac")
        w.write(b"\r\n")
        await w.drain()
        assert (await asyncio.wait_for(r.read(), 2)).startswith(b"HTTP/1.1 403")


@aio
async def test_broken_web_files_do_not_stop_buddyd(home, caplog):
    from cardbuddy import buddyd
    (home / "pki" / "server.crt").write_text("garbage")
    await buddyd._start_web(Hub(FakeLink()), 0)
    assert "web disabled" in caplog.text


@aio
async def test_host_match_ignores_case(home, monkeypatch):
    monkeypatch.setattr(webpki, "local_hostname", lambda: "Example-MacBook")
    webpki.init(renew=True)
    async with running(home) as (hub, port, _):
        assert (await request(port, ctx(home), "GET", "/", headers={"Host": f"Example-MacBook.local:{port}"}))[0] == 200
        st = (await request(port, ctx(home), "POST", "/api/perm_reply", {"req": "r", "decision": "deny"},
                            {"Host": f"Example-MacBook.local:{port}", "Origin": f"https://Example-MacBook.local:{port}"}))[0]
        assert st == 200


async def plain(port, host):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(f"GET /x HTTP/1.1\r\nHost: {host}\r\n\r\n".encode())
    await w.drain()
    raw = await asyncio.wait_for(r.read(), 5)
    w.close()
    head = raw.split(b"\r\n\r\n")[0].decode().split("\r\n")
    return head[0], {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in head[1:])}


@aio
async def test_plain_http_is_redirected_to_https(home, caplog):
    caplog.set_level("DEBUG", logger="cardbuddy.web")
    async with running(home) as (hub, port, _):
        status, h = await plain(port, f"localhost:{port}")
        assert status.startswith("HTTP/1.1 301") and h["location"] == f"https://localhost:{port}/"
        status, h = await plain(port, f"evil.example:{port}")
        assert h["location"] == f"https://127.0.0.1:{port}/"
        assert (await request(port, ctx(home), "GET", "/"))[0] == 200
    assert "connection from 127.0.0.1:" in caplog.text and "plain http" in caplog.text


@aio
async def test_redirects_do_not_leak_connection_slots(home, monkeypatch):
    monkeypatch.setattr(web, "MAX_CONN", 1)
    async with running(home) as (hub, port, _):
        for _ in range(3):
            assert (await plain(port, f"localhost:{port}"))[0].startswith("HTTP/1.1 301")
        assert (await request(port, ctx(home), "GET", "/"))[0] == 200


@aio
async def test_ask_reply_with_free_text(home):
    async with running(home) as (hub, port, _):
        hub.table.upsert(SID, "/w/proj", "t", "running", 0)
        req, _ = hub.table.add_ask(SID, [{"q": "どれ？", "h": "H", "o": ["A", "B"], "m": False},
                                         {"q": "複数", "h": "M", "o": ["X", "Y"], "m": True}])
        answers = [{"text": "どちらでもない"}, {"text": "X, 自由入力"}]
        st, _, resp = await request(port, ctx(home), "POST", "/api/ask_reply", {"req": req, "answers": answers})
        assert (st, json.loads(resp)) == (200, {"ok": True})
        assert hub.table.pop_events(SID) == [{"type": "ask_reply", "req": req, "answers": answers}]


def test_ui_offers_free_text_for_ask():
    js = (UI / "app.js").read_text()
    assert 'placeholder: "その他（自由入力）"' in js and "{ text: value }" in js


@aio
async def test_transcript_api(home, monkeypatch):
    from cardbuddy import transcript
    proj = home / "cc" / "projects" / "-w"
    proj.mkdir(parents=True)
    lines = [{"type": "user", "message": {"role": "user", "content": "架空"}},
             {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "返答"}]}}]
    (proj / f"{SID}.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines))
    monkeypatch.setenv("CARDBUDDY_TRANSCRIPTS", str(home / "cc" / "projects" / "*" / "{sid}.jsonl"))
    transcript.CACHE.clear()
    async with running(home) as (hub, port, _):
        c = ctx(home)
        assert (await request(port, c, "GET", f"/api/transcript?sid={SID}"))[0] == 404
        hub.table.upsert(SID, "/w", "", "running", 0)
        st, _, body = await request(port, c, "GET", f"/api/transcript?sid={SID}&limit=1")
        page = json.loads(body)
        assert st == 200 and (page["start"], page["end"], page["total"]) == (1, 2, 2)
        assert page["items"] == [{"line": 1, "kind": "assistant", "text": "返答"}]
        st, _, body = await request(port, c, "GET", f"/api/transcript?sid={SID}&before=1")
        assert [it["text"] for it in json.loads(body)["items"]] == ["架空"]
        st, _, body = await request(port, c, "GET", f"/api/transcript?sid={SID}&after=2")
        assert json.loads(body)["items"] == []
        assert (await request(port, c, "GET", f"/api/transcript?sid={SID}&after=x"))[0] == 400


@aio
async def test_activity_is_streamed_with_snapshot(home):
    async with running(home) as (hub, port, sock):
        await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w/proj", "title": "t", "state": "running"})
        await unix(sock, "POST", "/activity", {"sid": SID, "ev": {"type": "turn_start", "turn_id": "t1", "prompt": "p"}})
        await unix(sock, "POST", "/activity", {"sid": SID, "ev": {"type": "tool_start", "tool_use_id": "u1",
                                                                 "tool": "Bash", "summary": "ls"}})
        r, w = await open_sse(port, ctx(home))
        sessions, snap = await sse_events(r, 2)
        assert sessions["t"] == "sessions" and snap["t"] == "activity" and snap["sid"] == SID
        assert [e["type"] for e in snap["events"]] == ["turn_start", "tool_start"]
        assert [x["tool_use_id"] for x in snap["running"]] == ["u1"] and isinstance(snap["now"], float)
        await unix(sock, "POST", "/activity", {"sid": SID, "ev": {"type": "tool_end", "tool_use_id": "u1",
                                                                 "is_error": True, "ms": 12}})
        ev = (await sse_events(r, 1))[0]
        assert ev["t"] == "activity" and ev["sid"] == SID and "events" not in ev
        assert ev["item"]["type"] == "tool_end" and ev["item"]["is_error"] is True and "at" in ev["item"]
        w.close()


def test_ui_activity_script_is_served_before_app():
    html = (UI / "index.html").read_text()
    assert html.index('src="/activity.js"') < html.index('src="/app.js"')


@aio
async def test_sessions_carry_updated_at_and_are_republished(home, monkeypatch):
    import time as _time
    async with running(home) as (hub, port, sock):
        loop = asyncio.create_task(hub.sessions_loop())
        t0 = _time.time()
        await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w/a", "title": "t", "state": "running"})
        await unix(sock, "POST", "/session", {"sid": "other-sid", "cwd": "/w/b", "title": "t", "state": "running"})
        r, w = await open_sse(port, ctx(home))
        snap = (await sse_events(r, 1))[0]
        assert snap["t"] == "sessions" and isinstance(snap["now"], float)
        first = {s["sid"]: s["updated_at"] for s in snap["s"]}
        assert all(t0 <= v <= _time.time() for v in first.values())
        await asyncio.sleep(0.05)
        await unix(sock, "POST", "/session", {"sid": SID, "cwd": "/w/a", "title": "t", "state": "running"})
        assert hub.table.sessions[SID]["updated_at"] == first[SID]
        for path, body in (("/log", {"sid": SID, "role": "assistant", "text": "x"}),
                           ("/activity", {"sid": SID, "ev": {"type": "tool_start", "tool_use_id": "u", "tool": "Bash"}}),
                           ("/ask", {"sid": SID, "qs": [{"q": "q", "h": "h", "o": ["a", "b"], "m": False}]})):
            before = hub.table.sessions[SID]["updated_at"]
            await asyncio.sleep(0.01)
            await unix(sock, "POST", path, body)
            assert hub.table.sessions[SID]["updated_at"] > before, path
        while True:
            ev = (await sse_events(r, 1))[0]
            if ev["t"] == "sessions":
                break
        assert {s["sid"]: s["updated_at"] for s in ev["s"]}[SID] > first[SID]
        loop.cancel()
        w.close()


def test_ui_sorts_sessions_by_updated_at():
    js = (UI / "app.js").read_text()
    assert "(b.updated_at || 0) - (a.updated_at || 0)" in js and "list.guardUntil = Date.now() + 500" in js


def test_ui_sends_prompt_by_sid():
    js = (UI / "app.js").read_text()
    assert 'post("/api/prompt", { sid: s.sid, text })' in js


def test_ui_keeps_back_and_latest_buttons_in_sticky_header():
    html = (UI / "index.html").read_text()
    header = html[html.index("<header>"):html.index("</header>")]
    assert 'id="back"' in header and 'id="latest"' in header and 'id="h-badge"' in header
    css = (UI / "app.css").read_text()
    head_css = css[css.index("header {"):css.index("}", css.index("header {"))]
    assert "position: sticky" in head_css and "safe-area-inset-top" in head_css
    assert "safe-area-inset-bottom" in css
    js = (UI / "app.js").read_text()
    assert '$("latest").addEventListener' in js and 'style="' not in html


@aio
async def test_web_replies_are_audited_and_resolved_by_web(home, caplog):
    caplog.set_level("INFO", logger="cardbuddy.web")
    async with running(home) as (hub, port, sock):
        hub.table.upsert(SID, "/w/proj", "t", "running", 0)
        req, _ = hub.table.add_perm(SID, "Bash", {"command": "secret-command"}, 0)
        q = hub.web_subscribe()
        await request(port, ctx(home), "POST", "/api/perm_reply", {"req": req, "decision": "deny"})
        assert q.get_nowait() == {"t": "resolved", "req": req, "by": "web"}
        await request(port, ctx(home), "POST", "/api/prompt", {"sid": SID, "text": "ひみつの本文"})
        audit = [r.getMessage() for r in caplog.records if "audit" in r.getMessage()]
        assert len(audit) == 2
        assert f"kind=perm_reply req={req} decision=deny cn=mac" in audit[0] and "sha256=" in audit[0]
        assert "kind=prompt" in audit[1] and "ひみつ" not in caplog.text and "secret-command" not in caplog.text


@aio
async def test_preauth_logs_are_debug_and_plain_reads_headers_only(home, caplog):
    caplog.set_level("INFO", logger="cardbuddy.web")
    async with running(home) as (hub, port, _):
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(f"POST / HTTP/1.1\r\nHost: localhost:{port}\r\nContent-Length: 1000000\r\n\r\n".encode())
        await w.drain()
        assert (await asyncio.wait_for(r.read(), 2)).startswith(b"HTTP/1.1 301")
        w.close()
    assert "connection from" not in caplog.text and "plain http" not in caplog.text


@aio
async def test_connections_are_capped_per_ip(home, monkeypatch):
    monkeypatch.setattr(web, "PER_IP", 2)
    monkeypatch.setattr(web, "HANDSHAKE_TIMEOUT", 0.5)
    async with running(home) as (hub, port, _):
        idle = [await asyncio.open_connection("127.0.0.1", port) for _ in range(2)]
        await asyncio.sleep(0.05)
        with pytest.raises((ssl.SSLError, ConnectionError, asyncio.IncompleteReadError, IndexError)):
            await request(port, ctx(home), "GET", "/")
        for r, w in idle:
            w.close()
        await asyncio.sleep(0.1)
        assert (await request(port, ctx(home), "GET", "/"))[0] == 200


@aio
async def test_web_enabled_only_after_listening(home):
    hub = Hub(FakeLink())
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    with pytest.raises(OSError):
        await web.serve_web(hub, "127.0.0.1", port)
    assert hub.web_enabled is False
    blocker.close()


def test_ui_draws_edit_and_write_in_separate_sections():
    js = (UI / "app.js").read_text()
    assert 'section("変更前（削除）", d.old)' in js and 'section("変更後（追加）", d.new)' in js
    assert 'section("書き込む内容", d.content)' in js
