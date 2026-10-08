"""mod 向けの HTTP/1.1 over Unix socket（docs/daemon-api.md）。"""

import asyncio
import http
import json
import logging
import math
import os
import socket
import time
from urllib.parse import parse_qs, unquote, urlsplit

from .session_table import ROLES, STATES

log = logging.getLogger(__name__)

# Write / Edit の input は本文ごと届くので大きめに取る
MAX_BODY = 1024 * 1024
MAX_HEADERS = 100
READ_TIMEOUT = 10.0
# クライアント側の $.http.fetch が 30 秒で打ち切るため、待ちは必ずそれより前に返す
WAIT_DEFAULT = WAIT_MAX = 25
# type ごとのフィールド: (型, 最大長, 必須)。文字列は空を許さない（prompt だけは続きのターンで空になる）
ACTIVITY_FIELDS = {
    "turn_start": {"turn_id": (str, 64, True), "prompt": (str, 80, True)},
    "turn_end": {"turn_id": (str, 64, True), "reason": (str, 32, True), "ms": (int, None, True),
                 "agent_id": (str, 64, False), "agent_type": (str, 128, False)},
    "tool_start": {"tool_use_id": (str, 128, True), "tool": (str, 128, True), "summary": (str, 1024, False),
                   "agent_id": (str, 64, False), "agent_type": (str, 128, False)},
    "tool_end": {"tool_use_id": (str, 128, True), "is_error": (bool, None, True), "ms": (int, None, True)},
}


class HttpError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(msg)
        self.code = code


def _is_text(v) -> bool:
    # JSON の \ud83d のような孤立サロゲートは str として通るが、送信時の UTF-8 化で落ちる
    try:
        v.encode()
    except UnicodeEncodeError:
        return False
    return True


def _get(body: dict, key: str, typ, maxlen: int | None = None):
    v = body.get(key)
    if type(v) is not typ or (typ is str and not _is_text(v)) or (maxlen is not None and not 0 < len(v) <= maxlen):
        raise HttpError(400, f"bad {key}")
    return v


def _qs(qs) -> list[dict]:
    if not isinstance(qs, list) or not 1 <= len(qs) <= 4:
        raise HttpError(400, "bad qs")
    for q in qs:
        if not isinstance(q, dict):
            raise HttpError(400, "bad qs")
        _get(q, "q", str), _get(q, "h", str), _get(q, "m", bool)
        o = q.get("o")
        if not isinstance(o, list) or not 2 <= len(o) <= 4 or not all(isinstance(x, str) and _is_text(x) for x in o):
            raise HttpError(400, "bad qs.o")
    return qs


def _input(body: dict) -> dict:
    v = body.get("input")
    if not isinstance(v, dict) or not _is_text(json.dumps(v, ensure_ascii=False)):
        raise HttpError(400, "bad input")
    return v


def _activity(ev) -> dict:
    fields = ACTIVITY_FIELDS.get(ev.get("type")) if isinstance(ev, dict) else None
    if fields is None:
        raise HttpError(400, "bad ev")
    out = {"type": ev["type"]}
    for k, (typ, maxlen, required) in fields.items():
        if k not in ev and not required:
            continue
        v = ev.get(k)
        if typ is str:
            ok = type(v) is str and _is_text(v) and len(v) <= maxlen and (v != "" or k == "prompt")
        else:
            ok = type(v) is typ and (typ is bool or v >= 0)
        if not ok:
            raise HttpError(400, f"bad ev.{k}")
        out[k] = v
    return out


def _timeout(q: dict) -> float:
    try:
        t = float(q.get("timeout", [WAIT_DEFAULT])[0])
    except ValueError:
        raise HttpError(400, "bad timeout") from None
    if math.isnan(t):
        raise HttpError(400, "bad timeout")
    return min(max(t, 0.0), WAIT_MAX)


def _session(hub, sid: str) -> str:
    if sid not in hub.table.sessions:
        raise HttpError(404, "unknown session")
    return sid


async def _route(hub, reader, method: str, target: str, body: dict):
    url = urlsplit(target)
    p = url.path
    if method == "GET" and p == "/status":
        return {"connected": hub.link.connected, "transport": hub.link.transport, "device": hub.link.device,
                "sessions": hub.table.status()}
    if method == "GET" and p == "/poll":
        q = parse_qs(url.query)
        sid = q.get("sid", [""])[0]
        if not sid:
            raise HttpError(400, "bad sid")
        return {"events": await _poll(hub, reader, sid, _timeout(q))}
    if method == "GET" and p.startswith("/perm/"):
        req = unquote(p[len("/perm/"):])
        timeout = _timeout(parse_qs(url.query))
        try:
            hub.table.perm_result(req)
        except KeyError:
            raise HttpError(404, "unknown req") from None
        hub.table.perm_wait_begin(req)
        try:
            return await _wait(hub, reader, timeout, lambda: hub.table.perm_result(req)) or {}
        finally:
            hub.table.perm_wait_end(req, time.monotonic())
    if method == "DELETE" and p.startswith("/session/"):
        hub.send(hub.table.delete(unquote(p[len("/session/"):])))
        hub.kick()
        return {}
    if method != "POST":
        raise HttpError(404, "not found")
    if p == "/session":
        state = _get(body, "state", str)
        if state not in STATES:
            raise HttpError(400, "bad state")
        sid = _get(body, "sid", str, 128)
        had_n = hub.table.sessions.get(sid, {}).get("n") is not None
        n = hub.table.upsert(sid, _get(body, "cwd", str), _get(body, "title", str), state, time.monotonic())
        if not had_n:
            hub.send(hub.table.pending_msgs(sid=sid))
        hub.kick()
        return {"n": n}
    if p == "/perm":
        sid = _session(hub, _get(body, "sid", str, 128))
        tool, inp = _get(body, "tool", str), _input(body)
        # デバイスに出せない perm を作ると、perm hook が答えの来ない待ちを続けるだけになる
        if not hub.link.connected or hub.table.sessions[sid]["n"] is None:
            raise HttpError(503, "device unavailable")
        req, msgs = hub.table.add_perm(sid, tool, inp, time.monotonic())
        hub.send(msgs)
        return {"req": req}
    if p == "/tool_done":
        hub.send(hub.table.tool_done(_get(body, "sid", str, 128), _get(body, "tool", str), _input(body)))
        return {}
    if p == "/ask":
        sid = _session(hub, _get(body, "sid", str, 128))
        req, msgs = hub.table.add_ask(sid, _qs(body.get("qs")))
        hub.send(msgs)
        return {"req": req}
    if p == "/resolved":
        by = _get(body, "by", str)
        if by not in ("terminal", "abort"):
            raise HttpError(400, "bad by")
        hub.send(hub.table.resolve(_get(body, "req", str, 12), by))
        return {}
    if p == "/log":
        role = _get(body, "role", str)
        if role not in ROLES:
            raise HttpError(400, "bad role")
        hub.table.add_log(_get(body, "sid", str, 128), role, _get(body, "text", str))
        hub.kick()
        return {}
    if p == "/activity":
        sid, ev = _get(body, "sid", str, 128), _activity(body.get("ev"))
        item = hub.table.add_activity(sid, ev, time.time())
        if item is not None:
            hub.publish_activity(sid, item)
        return {}
    if p == "/ack_prompt":
        sid = _session(hub, _get(body, "sid", str, 128))
        hub.send(hub.table.ack_prompt(sid, _get(body, "queued", bool)))
        return {}
    raise HttpError(404, "not found")


async def _wait(hub, reader, timeout: float, check):
    """check() が None 以外を返すまで待つ。timeout かクライアント切断なら None。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        # 取り逃がしを防ぐため、check の前に待つ Event を掴む
        ev = hub.wake_event
        # クライアントが切断済みなら、pop すると届かず失われるので check しない
        if reader.at_eof():
            return None
        v = check()
        if v is not None:
            return v
        remaining = deadline - loop.time()
        if remaining <= 0:
            return None
        try:
            await asyncio.wait_for(ev.wait(), remaining)
        except asyncio.TimeoutError:
            return None


async def _poll(hub, reader, sid: str, timeout: float) -> list[dict]:
    gen = hub.polls[sid] = hub.polls.get(sid, 0) + 1
    hub.wake()

    def check():
        if hub.polls.get(sid) != gen:
            return []
        return hub.table.pop_events(sid) or None

    try:
        return await _wait(hub, reader, timeout, check) or []
    finally:
        if hub.polls.get(sid) == gen:
            del hub.polls[sid]


async def _read_request(reader) -> tuple[str, str, dict]:
    try:
        method, target, _ = (await reader.readline()).decode("ascii").split(" ", 2)
    except (UnicodeDecodeError, ValueError):
        raise HttpError(400, "bad request line") from None
    headers = {}
    for _ in range(MAX_HEADERS):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.decode("latin-1").partition(":")
        headers[k.strip().lower()] = v.strip()
    else:
        raise HttpError(431, "too many headers")
    if "transfer-encoding" in headers:
        raise HttpError(411, "content-length required")
    try:
        n = int(headers.get("content-length", "0"))
    except ValueError:
        raise HttpError(400, "bad content-length") from None
    if not 0 <= n <= MAX_BODY:
        raise HttpError(413, "body too large")
    raw = await reader.readexactly(n)
    if not raw:
        return method, target, {}
    try:
        body = json.loads(raw)
    except ValueError:
        raise HttpError(400, "bad json") from None
    if not isinstance(body, dict):
        raise HttpError(400, "body must be an object")
    return method, target, body


async def _handle(hub, reader, writer):
    try:
        try:
            method, target, body = await asyncio.wait_for(_read_request(reader), READ_TIMEOUT)
            code, resp = 200, await _route(hub, reader, method, target, body)
        except HttpError as e:
            code, resp = e.code, {"error": str(e)}
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError):
            code, resp = 400, {"error": "bad request"}
        data = json.dumps(resp).encode()
        writer.write(
            f"HTTP/1.1 {code} {http.HTTPStatus(code).phrase}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
        await writer.drain()
    except (ConnectionError, OSError) as e:
        log.debug("client gone: %s", e)
    except Exception:
        log.exception("http handler failed")
    finally:
        writer.close()


async def serve(hub, path: str) -> asyncio.base_events.Server:
    d = os.path.dirname(path)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    if os.path.exists(path):
        with socket.socket(socket.AF_UNIX) as s:
            try:
                s.connect(path)
            except OSError:
                os.unlink(path)
            else:
                raise SystemExit(f"buddyd is already running on {path}")
    server = await asyncio.start_unix_server(lambda r, w: _handle(hub, r, w), path)
    os.chmod(path, 0o600)
    return server
