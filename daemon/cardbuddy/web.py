"""LAN 向けの Web UI（iPhone の Safari から操作する）。mTLS で端末を絞る。

Safari は別のサイトを開いているときでも、この Mac への通信にクライアント証明書を付ける。
そのため mTLS だけでは CSRF を防げず、状態を変える API には専用ヘッダーと Origin の一致を必須にする。
"""

import asyncio
import hashlib
import http
import json
import logging
import socket
import ssl
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from . import transcript, webpki
from .http_api import READ_TIMEOUT, HttpError, _read_request
from .session_table import PROMPT_MAX

log = logging.getLogger(__name__)

WEB_PORT = 47825
HEARTBEAT = 15.0
# ハンドシェイク前の接続も数える（証明書を持たない LAN の端末が fd を使い切れないようにする）
MAX_CONN = 32
PER_IP = 8
REFUSED_LOG_EVERY = 60.0
HANDSHAKE_TIMEOUT = 10.0
UI = Path(__file__).parent / "webui"
STATIC = {"/": ("index.html", "text/html; charset=utf-8"),
          "/app.js": ("app.js", "text/javascript; charset=utf-8"),
          "/activity.js": ("activity.js", "text/javascript; charset=utf-8"),
          "/app.css": ("app.css", "text/css; charset=utf-8")}
SECURITY_HEADERS = ("Content-Security-Policy: default-src 'self'\r\n"
                    "X-Frame-Options: DENY\r\n"
                    "Cache-Control: no-store\r\n"
                    "X-Content-Type-Options: nosniff\r\n"
                    "Referrer-Policy: no-referrer\r\n")


def pki_ready() -> bool:
    d = webpki.pki_dir()
    return all((d / f).exists() for f in ("ca.crt", "server.crt", "server.key"))


def ssl_context() -> ssl.SSLContext:
    d = webpki.pki_dir()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.load_verify_locations(d / "ca.crt")
    ctx.load_cert_chain(d / "server.crt", d / "server.key")
    return ctx


async def serve_web(hub, host: str, port: int) -> asyncio.base_events.Server:
    ctx, names = ssl_context(), webpki.server_names()
    active: dict[str, int] = {}
    refused = {"n": 0, "at": 0.0}

    def refuse(writer, why: str):
        writer.close()
        # 認証前の接続はだれでも作れるので、1 件ずつログに出すとログで DoS できてしまう、件数でまとめる
        refused["n"] += 1
        now = asyncio.get_running_loop().time()
        if now - refused["at"] >= REFUSED_LOG_EVERY:
            log.warning("web: refused %d connection(s) in the last %.0fs (%s)", refused["n"], REFUSED_LOG_EVERY, why)
            refused.update(n=0, at=now)

    async def accept(reader, writer):
        # start_server(ssl=...) はハンドシェイク後にしか呼ばれず上限を掛けられないので、平文で受けてから TLS にする
        ip, port_ = writer.get_extra_info("peername")[:2]
        peer = f"{ip}:{port_}"
        log.debug("web: connection from %s", peer)
        if sum(active.values()) >= MAX_CONN:
            return refuse(writer, f"{MAX_CONN} connections open")
        if active.get(ip, 0) >= PER_IP:
            return refuse(writer, f"{PER_IP} connections from {ip}")
        active[ip] = active.get(ip, 0) + 1
        try:
            try:
                first = await _peek(writer)
                if first != b"\x16":
                    await _redirect(names, reader, writer, peer, first)
                    return
                await writer.start_tls(ctx, ssl_handshake_timeout=HANDSHAKE_TIMEOUT)
            except Exception as e:
                log.debug("web: %s dropped before TLS: %r", peer, e)
                writer.close()
                return
            log.debug("web: tls established with %s", peer)
            await _handle(hub, names, reader, writer)
        finally:
            active[ip] -= 1
            if not active[ip]:
                del active[ip]

    server = await asyncio.start_server(accept, host, port)
    hub.web_enabled = True
    return server


async def _peek(writer) -> bytes:
    """先頭の 1 byte を消費せずに読む（0x16 なら TLS の ClientHello）。"""
    # 読んでしまうと start_tls に ClientHello を渡せないので、トランスポートを止めて複製したソケットで覗く
    writer.transport.pause_reading()
    sock = writer.get_extra_info("socket").dup()
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    loop.add_reader(sock.fileno(), lambda: ready.done() or ready.set_result(None))
    try:
        await asyncio.wait_for(ready, HANDSHAKE_TIMEOUT)
        return sock.recv(1, socket.MSG_PEEK)
    finally:
        loop.remove_reader(sock.fileno())
        sock.close()


async def _redirect(names: set[str], reader, writer, peer: str, first: bytes):
    if not first:
        writer.close()
        return
    writer.transport.resume_reading()
    try:
        host = await asyncio.wait_for(_read_host(reader), READ_TIMEOUT)
    except (asyncio.TimeoutError, ValueError, ConnectionError):
        host = None
    local, port = writer.get_extra_info("sockname")[:2]
    # 転送先は証明書の名前に限る（それ以外の Host は、接続を受けた IP の URL に転送する）
    if host not in names:
        host = local if local in names else next((n for n in sorted(names) if n[0].isdigit() and n != "127.0.0.1"), local)
    url = f"https://{host}:{port}/"
    log.debug("web: plain http from %s, redirect to %s", peer, url)
    writer.write(f"HTTP/1.1 301 Moved Permanently\r\nLocation: {url}\r\nContent-Length: 0\r\n"
                 f"{SECURITY_HEADERS}Connection: close\r\n\r\n".encode())
    await writer.drain()
    writer.close()


async def _read_host(reader) -> str | None:
    """平文の要求からヘッダーだけを読み、Host の名前を返す（本文は読まない）。"""
    host = None
    for _ in range(100):
        line = await reader.readline()
        if line in (b"\r\n", b"\n", b""):
            break
        k, _, v = line.decode("latin-1").partition(":")
        if k.strip().lower() == "host":
            host = urlsplit("//" + v.strip()).hostname
    return host


def _enrolled(fp: str) -> bool:
    # 再起動なしで revoke を反映するため、許可リストは毎回読み直す
    return fp in webpki.load_allowed()


def _peer(writer) -> tuple[str, str]:
    sslobj = writer.get_extra_info("ssl_object")
    fp = hashlib.sha256(sslobj.getpeercert(binary_form=True)).hexdigest()
    cn = next((v for rdn in sslobj.getpeercert().get("subject", ()) for k, v in rdn if k == "commonName"), "?")
    return fp, cn


def _head(code: int, ctype: str, length: int | None = None) -> bytes:
    size = f"Content-Length: {length}\r\n" if length is not None else ""
    return (f"HTTP/1.1 {code} {http.HTTPStatus(code).phrase}\r\nContent-Type: {ctype}\r\n{size}"
            f"{SECURITY_HEADERS}Connection: close\r\n\r\n").encode()


def _json(code: int, obj: dict) -> bytes:
    data = json.dumps(obj, ensure_ascii=False).encode()
    return _head(code, "application/json; charset=utf-8", len(data)) + data


def _check_csrf(headers: dict[str, str]):
    if not headers.get("content-type", "").startswith("application/json"):
        raise HttpError(403, "content-type must be application/json")
    if headers.get("x-cardbuddy") != "1":
        raise HttpError(403, "missing X-CardBuddy header")
    host = headers.get("host")
    if not host or headers.get("origin", "").lower() != f"https://{host}".lower():
        raise HttpError(403, "origin mismatch")


def _field(body: dict, key: str, typ):
    v = body.get(key)
    if type(v) is not typ:
        raise HttpError(400, f"bad {key}")
    return v


def _audit(who: tuple[str, str], kind: str, **fields):
    # だれがどの要求に答えたかを残す（本文は残さない）
    extra = "".join(f" {k}={v}" for k, v in fields.items())
    log.info("web: audit kind=%s%s cn=%s sha256=%s", kind, extra, who[1], who[0][:16])


def _reply(hub, who, kind: str, body: dict) -> dict:
    req = _field(body, "req", str)
    if kind == "perm_reply":
        msg = {"t": kind, "req": req, "decision": _field(body, "decision", str)}
        _audit(who, kind, req=req, decision=msg["decision"])
    else:
        msg = {"t": kind, "req": req, "answers": _field(body, "answers", list)}
        _audit(who, kind, req=req)
    pending = req in hub.table.reqs
    # デバイスからの返信と同じ検証の経路を通す（未解決でない req や不正な値は表の側で捨てられる）
    hub.on_msg(msg, by="web")
    return {"ok": pending and req not in hub.table.reqs}


def _prompt(hub, who, body: dict) -> dict:
    text = _field(body, "text", str)
    _audit(who, "prompt", sid=body.get("sid") if isinstance(body.get("sid"), str) else body.get("id"))
    if not 0 < len(text) <= PROMPT_MAX:
        raise HttpError(400, f"text must be 1 to {PROMPT_MAX} characters")
    # 番号の無い（10 件目以降の）セッションにも送れるよう、Web からは sid で指定できる
    if "sid" in body:
        if not hub.table.queue_prompt(_field(body, "sid", str), text):
            raise HttpError(404, "no such session")
        hub.wake()
        return {"ok": True}
    n, sid8 = _field(body, "n", int), _field(body, "id", str)
    # 一致しないときに on_msg を通すと、デバイスに ack_prompt{ok:false} が飛ぶので先に弾く
    if hub.table._match(n, sid8) is None:
        raise HttpError(404, "no such session")
    hub.on_msg({"t": "prompt", "n": n, "id": sid8, "text": text}, by="web")
    return {"ok": True}


def _event(ev: dict) -> bytes:
    return b"data: " + json.dumps(ev, ensure_ascii=False).encode() + b"\n\n"


async def _sse(hub, reader, writer, fp: str):
    q = hub.web_subscribe()
    getter = None
    try:
        writer.write(_head(200, "text/event-stream; charset=utf-8"))
        for ev in hub.web_snapshot():
            writer.write(_event(ev))
        await writer.drain()
        while q in hub.web_subs and not reader.at_eof():
            # wait_for(q.get()) はタイムアウトと同時に取り出したイベントを失うことがあるので、取り出しのタスクを持ち越す
            getter = getter or asyncio.create_task(q.get())
            done, _ = await asyncio.wait({getter}, timeout=HEARTBEAT)
            if not _enrolled(fp):
                return
            if done:
                writer.write(_event(getter.result()))
                getter = None
            else:
                # Safari は無通信のストリームを切るので、コメント行で生存を伝える
                writer.write(b": keepalive\n\n")
            await writer.drain()
    finally:
        if getter:
            getter.cancel()
        hub.web_unsubscribe(q)


async def _route(hub, reader, writer, who: tuple[str, str], method: str, target: str, headers: dict,
                 body: dict) -> int:
    fp = who[0]
    path = urlsplit(target).path
    # CORS の preflight（OPTIONS）に応じないことで、別オリジンから専用ヘッダー付きの要求を送らせない
    if method == "OPTIONS":
        raise HttpError(403, "cross-origin requests are not allowed")
    if method == "GET" and path in STATIC:
        name, ctype = STATIC[path]
        data = (UI / name).read_bytes()
        writer.write(_head(200, ctype, len(data)) + data)
        return 200
    if method == "GET" and path == "/api/events":
        await _sse(hub, reader, writer, fp)
        return 200
    if method == "GET" and path == "/api/transcript":
        q = parse_qs(urlsplit(target).query)
        try:
            args = {k: int(q[k][0]) for k in ("before", "after", "limit") if k in q}
        except ValueError:
            raise HttpError(400, "before / after / limit must be integers") from None
        # 初回は索引のために全体を読むので、イベントループの外で読む
        page = await asyncio.to_thread(transcript.read, hub.table, q.get("sid", [""])[0], **args)
        if page is None:
            raise HttpError(404, "no transcript for this session")
        writer.write(_json(200, page))
        return 200
    if method == "GET" and path.startswith("/api/sessions/") and path.endswith("/log"):
        items = hub.table.web_log(unquote(path[len("/api/sessions/"):-len("/log")]))
        if items is None:
            raise HttpError(404, "no such session")
        writer.write(_json(200, {"items": items}))
        return 200
    handlers = {"/api/perm_reply": lambda b: _reply(hub, who, "perm_reply", b),
                "/api/ask_reply": lambda b: _reply(hub, who, "ask_reply", b),
                "/api/prompt": lambda b: _prompt(hub, who, b)}
    if path in handlers:
        if method != "POST":
            raise HttpError(405, "use POST")
        _check_csrf(headers)
        writer.write(_json(200, handlers[path](body)))
        return 200
    raise HttpError(404, "not found")


async def _handle(hub, names: set[str], reader, writer):
    fp = cn = "?"
    try:
        fp, cn = _peer(writer)
        if not _enrolled(fp):
            log.warning("web: refused cn=%s sha256=%s (not enrolled)", cn, fp[:16])
            writer.write(_json(403, {"error": "this device is not enrolled"}))
            return
        method = target = "?"
        try:
            method, target, headers, body = await asyncio.wait_for(_read_request(reader), READ_TIMEOUT)
            # DNS リバインディングでは Host と Origin がそろって攻撃者の名前になるので、証明書の名前に限る
            if urlsplit("//" + headers.get("host", "")).hostname not in names:
                raise HttpError(403, "unknown host")
            if not _enrolled(fp):
                raise HttpError(403, "this device is not enrolled")
            code = await _route(hub, reader, writer, (fp, cn), method, target, headers, body)
        except HttpError as e:
            code = e.code
            writer.write(_json(code, {"error": str(e)}))
        log.info("web: %s %s %d cn=%s sha256=%s", method, urlsplit(target).path, code, cn, fp[:16])
        await writer.drain()
    except (asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError, ConnectionError, ssl.SSLError) as e:
        log.info("web: dropped cn=%s sha256=%s: %r", cn, fp[:16], e)
    except Exception:
        log.exception("web handler failed")
    finally:
        writer.close()
