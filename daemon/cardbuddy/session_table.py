"""セッション表と未解決要求。I/O を持たず、時刻は引数で受け取る。"""

import hashlib
import json
import logging
import os
import secrets
from collections import deque

from .crypto import MAX_PLAINTEXT

log = logging.getLogger(__name__)

TTL = 30.0
# perm hook は 25 秒ごとに GET し直すので、これを超えて待ちが無い perm は hook が死んだとみなす
PERM_IDLE = 60.0
MAX_N = 9
STATES = ("running", "idle")
PROMPT_MAX = 500
# ponytail: 結果は件数で古い順に捨てる、perm hook が 25 秒ごとに取りに来る前提で十分な数
MAX_RESULTS = 256
LOG_KEEP, LOG_TEXT_MAX, LOG_CHUNK = 20, 4000, 500
ROLES = {"user": "u", "assistant": "a"}


def trunc(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def perm_hint(tool: str, inp: dict) -> str:
    def has(*keys):
        return all(isinstance(inp.get(k), str) for k in keys)

    if tool == "Bash" and has("command"):
        return inp["command"]
    if tool == "Edit" and has("file_path", "old_string", "new_string"):
        head = inp["file_path"] + (" (replace_all)" if inp.get("replace_all") is True else "")
        return f"{head}\n- {inp['old_string']}\n+ {inp['new_string']}"
    if tool == "Write" and has("file_path", "content"):
        return f"{inp['file_path']}\n{inp['content']}"
    return _json(inp)


def _pt_size(msg: dict) -> int:
    return len(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())


def _size(kind: str, fields: dict) -> int:
    # n と req は桁数が固定なので、仮の値で seal と同じ直列化の長さを測れる
    probe = {"t": kind, "n": 9, "req": "r" + "0" * 10, **fields}
    return len(json.dumps(probe, ensure_ascii=False, separators=(",", ":")).encode())


def _input_key(tool: str, inp: dict) -> bytes:
    return hashlib.sha256(_json([tool, inp]).encode()).digest()


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


class SessionTable:
    def __init__(self):
        self.sessions: dict[str, dict] = {}  # sid -> {n, cwd, title, state, seen}
        self.reqs: dict[str, dict] = {}  # req -> {sid, kind, fields, qs | key, waiters, idle_since}
        self.queues: dict[str, list] = {}
        self.logs: dict[str, deque] = {}
        self.perm_results: dict[str, dict] = {}  # 解決済み perm の {decision} / {released}

    def upsert(self, sid: str, cwd: str, title: str, state: str, now: float) -> int | None:
        s = self.sessions.get(sid)
        if s is None:
            s = self.sessions[sid] = {"n": None}
            self.queues[sid] = []
            self.logs[sid] = deque(maxlen=LOG_KEEP)
        if s["n"] is None:
            used = {x["n"] for x in self.sessions.values()}
            s["n"] = next((n for n in range(1, MAX_N + 1) if n not in used), None)
        s.update(cwd=cwd, title=title, state=state, seen=now)
        return s["n"]

    def delete(self, sid: str) -> list[dict]:
        if self.sessions.pop(sid, None) is None:
            return []
        self.queues.pop(sid, None)
        self.logs.pop(sid, None)
        out = []
        for req in [r for r, v in self.reqs.items() if v["sid"] == sid]:
            out += self.resolve(req, "abort")
        return out

    def expire(self, now: float) -> list[dict]:
        out = []
        for sid in [k for k, s in self.sessions.items() if now - s["seen"] > TTL]:
            log.info("session %s expired", sid)
            out += self.delete(sid)
        for req in [k for k, r in self.reqs.items()
                    if r["kind"] == "perm" and not r["waiters"] and now - r["idle_since"] > PERM_IDLE]:
            log.info("perm %s abandoned by its hook", req)
            out += self.resolve(req, "abort")
        return out

    def _state(self, sid: str) -> str:
        kinds = {r["kind"] for r in self.reqs.values() if r["sid"] == sid}
        return "ask" if "ask" in kinds else "perm" if "perm" in kinds else self.sessions[sid]["state"]

    def status(self) -> list[dict]:
        return [{"n": s["n"], "sid": sid, "state": self._state(sid)} for sid, s in self.sessions.items()]

    def _who(self, sid: str) -> dict:
        return {"id": sid[:8], "name": trunc(os.path.basename(self.sessions[sid]["cwd"].rstrip("/")), 16)}

    def _last(self, sid: str) -> str:
        it = next((it for it in reversed(self.logs[sid]) if it["r"] == "a"), None)
        return it["x"].split("\n", 1)[0] if it else ""

    def sessions_msg(self) -> dict:
        rows = sorted((s["n"], sid, s) for sid, s in self.sessions.items() if s["n"] is not None)
        tl, ll = 24, 40
        while True:
            msg = {"t": "sessions", "s": [{
                "n": n, **self._who(sid),
                "title": trunc(s["title"].split("\n", 1)[0], tl),
                "state": self._state(sid),
                "last": trunc(self._last(sid), ll),
            } for n, sid, s in rows]}
            if _pt_size(msg) <= MAX_PLAINTEXT or tl <= 1:
                return msg
            tl, ll = max(tl // 2, 1), max(ll // 2, 1)

    def _add(self, sid: str, kind: str, fields: dict, **extra) -> tuple[str, list[dict]]:
        if sid not in self.sessions:
            raise KeyError(sid)
        req = "r" + secrets.token_hex(5)
        self.reqs[req] = {"sid": sid, "kind": kind, "fields": fields, **extra}
        return req, self.pending_msgs(req=req)

    def add_perm(self, sid: str, tool: str, inp: dict, now: float) -> tuple[str, list[dict]]:
        desc = inp.get("description") if tool == "Bash" else None
        base = {**self._who(sid), "tool": tool, "desc": trunc(desc, 80) if isinstance(desc, str) else ""}
        hint = perm_hint(tool, inp)
        fields = {**base, "hint": hint, "full": True}
        if _size("perm", fields) > MAX_PLAINTEXT:
            # 収まる最長の hint[:k] + "…" を二分探索する、k = -1 は "…" すら入らない場合
            lo, hi = -1, len(hint) - 1
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if _size("perm", {**base, "hint": hint[:mid] + "…", "full": False}) <= MAX_PLAINTEXT:
                    lo = mid
                else:
                    hi = mid - 1
            fields = {**base, "hint": hint[:lo] + "…" if lo >= 0 else "", "full": False}
        return self._add(sid, "perm", fields, key=_input_key(tool, inp), waiters=0, idle_since=now)

    def perm_wait_begin(self, req: str):
        if req in self.reqs:
            self.reqs[req]["waiters"] += 1

    def perm_wait_end(self, req: str, now: float):
        r = self.reqs.get(req)
        if r is not None:
            r["waiters"] -= 1
            r["idle_since"] = now

    def add_ask(self, sid: str, qs: list[dict]) -> tuple[str, list[dict]]:
        ql, ol = 120, 40
        while True:
            short = [{"q": trunc(q["q"], ql), "h": trunc(q["h"], 12),
                      "o": [trunc(o, ol) for o in q["o"]], "m": q["m"]} for q in qs]
            fields = {**self._who(sid), "qs": short}
            if _size("ask", fields) <= MAX_PLAINTEXT or ql <= 1:
                break
            ql, ol = max(ql // 2, 1), max(ol // 2, 1)
        return self._add(sid, "ask", fields, qs=qs)

    def resolve(self, req: str, by: str, decision: str | None = None) -> list[dict]:
        r = self.reqs.pop(req, None)
        if r is None:
            return []
        if r["kind"] == "perm":
            self.perm_results[req] = {"decision": decision} if decision else {"released": True}
            while len(self.perm_results) > MAX_RESULTS:
                del self.perm_results[next(iter(self.perm_results))]
        return [{"t": "resolved", "req": req, "by": by}]

    def perm_result(self, req: str) -> dict | None:
        """解決済みなら結果、未解決なら None。perm でない・未知なら KeyError。"""
        if req in self.perm_results:
            return self.perm_results[req]
        if self.reqs[req]["kind"] != "perm":
            raise KeyError(req)
        return None

    def tool_done(self, sid: str, tool: str, inp: dict) -> list[dict]:
        key = _input_key(tool, inp)
        req = next((k for k, r in self.reqs.items()
                    if r["kind"] == "perm" and r["sid"] == sid and r["key"] == key), None)
        return self.resolve(req, "terminal") if req else []

    def pending_msgs(self, sid: str | None = None, req: str | None = None) -> list[dict]:
        out = []
        for k, r in self.reqs.items():
            n = self.sessions[r["sid"]]["n"]
            if n is not None and sid in (None, r["sid"]) and req in (None, k):
                out.append({"t": r["kind"], "n": n, "req": k, **r["fields"]})
        return out

    def ack_prompt(self, sid: str, queued: bool) -> list[dict]:
        n = self.sessions[sid]["n"]
        return [{"t": "ack_prompt", "n": n, "ok": True, "queued": queued}] if n is not None else []

    def add_log(self, sid: str, role: str, text: str):
        if sid in self.logs and text:
            self.logs[sid].append({"r": ROLES[role], "x": trunc(text, LOG_TEXT_MAX)})

    def pop_events(self, sid: str) -> list[dict]:
        q = self.queues.get(sid)
        if not q:
            return []
        self.queues[sid] = []
        return q

    def handle_device(self, msg: dict) -> list[dict]:
        t = msg.get("t")
        if t in ("perm_reply", "ask_reply"):
            return self._reply(t, msg)
        if t == "prompt":
            return self._prompt(msg)
        if t == "log_req":
            return self._log_req(msg)
        log.warning("drop device message: unknown t=%r", t)
        return []

    def _reply(self, t: str, msg: dict) -> list[dict]:
        req = msg.get("req")
        r = self.reqs.get(req) if isinstance(req, str) else None
        if r is None or r["kind"] + "_reply" != t:
            log.info("drop %s for non-pending req %r", t, req)
            return []
        if t == "perm_reply":
            if msg.get("decision") not in ("allow", "deny"):
                log.warning("drop perm_reply: bad decision")
                return []
            return self.resolve(req, "device", msg["decision"])
        if not _valid_answers(msg.get("answers"), r["qs"]):
            log.warning("drop ask_reply: bad answers")
            return []
        self.queues[r["sid"]].append({"type": t, "req": req, "answers": msg["answers"]})
        return self.resolve(req, "device")

    def _match(self, n: int, sid8: str) -> str | None:
        return next((k for k, s in self.sessions.items() if s["n"] == n and k[:8] == sid8), None)

    def _log_req(self, msg: dict) -> list[dict]:
        n, sid8, p = msg.get("n"), msg.get("id"), msg.get("p", 0)
        if not (_is_int(n) and isinstance(sid8, str) and _is_int(p) and p >= 0):
            log.warning("drop log_req: bad fields")
            return []
        sid = self._match(n, sid8)
        frags = [{"r": it["r"], "x": it["x"][i:i + LOG_CHUNK], "c": i > 0}
                 for it in (self.logs[sid] if sid else ()) for i in range(0, len(it["x"]), LOG_CHUNK)]
        # ponytail: 毎回全ページを先頭から作り直す、断片は最大 20 件 × 8 個なので十分速い
        end, page = len(frags), 0
        while end > 0:
            def fits(start):
                probe = {"t": "log", "n": n, "p": page, "more": False, "items": frags[start:end]}
                return _pt_size(probe) <= MAX_PLAINTEXT

            start = end
            while start > 0 and fits(start - 1):
                start -= 1
            if start == end:
                x = frags[end - 1]["x"]
                if len(x) > 1:
                    f = frags[end - 1]
                    half = len(x) // 2
                    frags[end - 1:end] = [{**f, "x": x[:half]}, {**f, "x": x[half:], "c": True}]
                    end += 1
                    continue
                start = end - 1
            if page == p:
                return [{"t": "log", "n": n, "p": p, "more": start > 0, "items": frags[start:end]}]
            end, page = start, page + 1
        return [{"t": "log", "n": n, "p": p, "more": False, "items": []}]

    def _prompt(self, msg: dict) -> list[dict]:
        n, sid8, text = msg.get("n"), msg.get("id"), msg.get("text")
        if not (_is_int(n) and isinstance(sid8, str) and isinstance(text, str) and len(text) <= PROMPT_MAX):
            log.warning("drop prompt: bad fields")
            return []
        sid = self._match(n, sid8)
        if sid is None:
            log.info("prompt for n=%d id=%s matches no session", n, sid8)
            return [{"t": "ack_prompt", "n": n, "ok": False, "queued": False}]
        self.queues[sid].append({"type": "prompt", "text": text})
        return []


def _valid_answers(answers, qs) -> bool:
    if not isinstance(answers, list) or len(answers) != len(qs):
        return False
    for a, q in zip(answers, qs):
        if not isinstance(a, list) or not a or len(set(map(repr, a))) != len(a):
            return False
        if not q["m"] and len(a) != 1:
            return False
        if not all(_is_int(i) and 0 <= i < len(q["o"]) for i in a):
            return False
    return True
