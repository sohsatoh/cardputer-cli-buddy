from cardbuddy.session_table import TTL, SessionTable, trunc

SID = "0f3c9a1e-aaaa-bbbb-cccc-000000000001"
QS = [{"q": "どれ？", "h": "Approach", "o": ["A", "B", "C"], "m": False},
      {"q": "複数", "h": "Multi", "o": ["x", "y"], "m": True}]


def sid(i):
    return f"{i:08x}-sess"


def test_trunc():
    assert trunc("abc", 3) == "abc"
    assert trunc("abcd", 3) == "ab…"
    assert trunc("あいうえお", 4) == "あいう…"


def test_upsert_assigns_smallest_free_n_and_keeps_it():
    t = SessionTable()
    assert [t.upsert(sid(i), "/x", "", "idle", 0) for i in range(1, 4)] == [1, 2, 3]
    t.delete(sid(2))
    assert t.upsert(sid(9), "/x", "", "idle", 1) == 2
    assert t.upsert(sid(1), "/y", "t", "running", 2) == 1


def test_tenth_session_gets_none_until_a_slot_frees():
    t = SessionTable()
    for i in range(9):
        t.upsert(sid(i), "/x", "", "idle", 0)
    assert t.upsert("extra", "/x", "", "idle", 0) is None
    assert len(t.sessions_msg()["s"]) == 9
    t.delete(sid(4))
    assert t.upsert("extra", "/x", "", "idle", 1) == 5


def test_sessions_msg_fields_and_truncation():
    t = SessionTable()
    t.upsert(SID, "/home/u/cardputer-cli-buddy-very-long", "実装して" + "x" * 30 + "\n二行目", "running", 0)
    assert t.sessions_msg() == {"t": "sessions", "s": [{
        "n": 1, "id": "0f3c9a1e", "name": "cardputer-cli-b…",
        "title": "実装して" + "x" * 19 + "…", "state": "running", "last": ""}]}


def test_ttl_expiry_aborts_pending_requests():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    t.upsert(sid(2), "/x", "", "idle", 0)
    req, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    t.upsert(sid(2), "/x", "", "idle", 20)
    assert t.expire(TTL) == []
    assert t.expire(TTL + 0.1) == [{"t": "resolved", "req": req, "by": "abort"}]
    assert [s["sid"] for s in t.status()] == [sid(2)]
    assert t.pending_msgs() == []


def test_delete_aborts_and_clears_queue():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    req, _ = t.add_ask(SID, QS)
    assert t.delete(SID) == [{"t": "resolved", "req": req, "by": "abort"}]
    assert t.delete(SID) == []
    assert t.pop_events(SID) == []


def test_perm_and_ask_messages_are_truncated():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    req, msgs = t.add_perm(SID, "T" * 30, {"file_path": "h" * 200, "description": "d" * 90}, 0)
    assert req.isalnum() and len(req) <= 12
    assert msgs == [{"t": "perm", "n": 1, "req": req, "id": SID[:8], "name": "x", "tool": "T" * 30, "desc": "",
                     "hint": '{"description":"' + "d" * 90 + '","file_path":"' + "h" * 200 + '"}', "full": True}]
    req2, msgs = t.add_ask(SID, [{"q": "q" * 130, "h": "header-too-long", "o": ["o" * 50, "b"], "m": False}])
    assert req2 != req
    assert msgs == [{"t": "ask", "n": 1, "req": req2, "id": SID[:8], "name": "x", "qs": [
        {"q": "q" * 119 + "…", "h": "header-too-…", "o": ["o" * 39 + "…", "b"], "m": False}]}]
    assert [m["t"] for m in t.pending_msgs()] == ["perm", "ask"]


def test_requests_of_unnumbered_session_are_not_sent():
    t = SessionTable()
    for i in range(9):
        t.upsert(sid(i), "/x", "", "idle", 0)
    t.upsert("extra", "/x", "", "idle", 0)
    req, msgs = t.add_perm("extra", "Bash", {"command": "ls"}, 0)
    assert req and msgs == []
    assert t.pending_msgs() == []
    assert t.ack_prompt("extra", True) == []


def test_unknown_sid_raises_keyerror():
    t = SessionTable()
    for f in (lambda: t.add_perm("nope", "a", {}, 0), lambda: t.add_ask("nope", QS),
              lambda: t.ack_prompt("nope", True)):
        try:
            f()
        except KeyError:
            continue
        raise AssertionError("expected KeyError")


def test_resolved_by_terminal():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    req, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    assert t.resolve(req, "terminal") == [{"t": "resolved", "req": req, "by": "terminal"}]
    assert t.resolve(req, "terminal") == []
    assert t.pending_msgs() == []


def test_perm_reply_accepted_once():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    req, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    out = t.handle_device({"t": "perm_reply", "req": req, "decision": "allow"})
    assert out == [{"t": "resolved", "req": req, "by": "device"}]
    assert t.perm_result(req) == {"decision": "allow"}
    assert t.pop_events(SID) == []
    assert t.handle_device({"t": "perm_reply", "req": req, "decision": "deny"}) == []
    assert t.pop_events(SID) == []


def test_perm_reply_rejects_bad_input():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    preq, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    areq, _ = t.add_ask(SID, QS)
    for m in ({"t": "perm_reply", "req": preq, "decision": "maybe"},
              {"t": "perm_reply", "req": areq, "decision": "allow"},
              {"t": "perm_reply", "req": "unknown", "decision": "allow"},
              {"t": "perm_reply", "req": ["x"], "decision": "allow"},
              {"t": "perm_reply"}):
        assert t.handle_device(m) == []
    assert t.pop_events(SID) == []
    assert len(t.pending_msgs()) == 2


def test_ask_reply_validation():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    req, _ = t.add_ask(SID, QS)
    for answers in ([[0]], [[0], [0], [1]], [[0, 1], [0]], [[3], [0]], [[], [0]],
                    [[True], [0]], [[0], [0, 0]], [0, [1]], "x", [[-1], [0]]):
        assert t.handle_device({"t": "ask_reply", "req": req, "answers": answers}) == [], answers
    assert t.pop_events(SID) == []
    out = t.handle_device({"t": "ask_reply", "req": req, "answers": [[2], [1, 0]]})
    assert out == [{"t": "resolved", "req": req, "by": "device"}]
    assert t.pop_events(SID) == [{"type": "ask_reply", "req": req, "answers": [[2], [1, 0]]}]


def test_prompt_requires_n_and_id_match():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    assert t.handle_device({"t": "prompt", "n": 1, "id": "deadbeef", "text": "hi"}) == [
        {"t": "ack_prompt", "n": 1, "ok": False, "queued": False}]
    assert t.handle_device({"t": "prompt", "n": 2, "id": "0f3c9a1e", "text": "hi"}) == [
        {"t": "ack_prompt", "n": 2, "ok": False, "queued": False}]
    assert t.pop_events(SID) == []
    assert t.handle_device({"t": "prompt", "n": 1, "id": "0f3c9a1e", "text": "hi"}) == []
    assert t.pop_events(SID) == [{"type": "prompt", "text": "hi"}]
    assert t.ack_prompt(SID, True) == [{"t": "ack_prompt", "n": 1, "ok": True, "queued": True}]


def test_prompt_type_errors_are_dropped():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    for m in ({"t": "prompt", "n": True, "id": "0f3c9a1e", "text": "hi"},
              {"t": "prompt", "n": 1, "id": "0f3c9a1e", "text": 3},
              {"t": "prompt", "n": 1, "id": "0f3c9a1e", "text": "x" * 501},
              {"t": "nope"}, {"x": 1}):
        assert t.handle_device(m) == []
    assert t.pop_events(SID) == []


def test_perm_hint_from_input():
    from cardbuddy.session_table import perm_hint
    assert perm_hint("Bash", {"command": "rm -rf ./build", "description": "x"}) == "rm -rf ./build"
    assert perm_hint("Edit", {"file_path": "/a.py", "old_string": "x = 1", "new_string": "x = 2"}) == (
        "/a.py\n- x = 1\n+ x = 2")
    assert perm_hint("Edit", {"file_path": "/a.py", "old_string": "a", "new_string": "b", "replace_all": True}) == (
        "/a.py (replace_all)\n- a\n+ b")
    assert perm_hint("Write", {"file_path": "/a.txt", "content": "line1\nline2"}) == "/a.txt\nline1\nline2"
    assert perm_hint("Edit", {"file_path": "/a.py"}) == '{"file_path":"/a.py"}'
    assert perm_hint("NotebookEdit", {"notebook_path": "/a.ipynb", "new_source": ""}) == (
        '{"new_source":"","notebook_path":"/a.ipynb"}')
    assert perm_hint("Bash", {"command": 3}) == '{"command":3}'


def test_perm_desc_only_for_bash():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    _, msgs = t.add_perm(SID, "Bash", {"command": "ls", "description": "d" * 90}, 0)
    assert msgs[0]["desc"] == "d" * 79 + "…"
    _, msgs = t.add_perm(SID, "Edit", {"file_path": "/a", "old_string": "", "new_string": "", "description": "d"}, 0)
    assert msgs[0]["desc"] == ""


def _pt(msg):
    import json
    return len(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())


def test_perm_full_flag_and_truncation_to_plaintext_limit():
    from cardbuddy.crypto import MAX_PLAINTEXT
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    content = "あ" * 600
    _, msgs = t.add_perm(SID, "Write", {"file_path": "/a", "content": content}, 0)
    assert msgs[0]["full"] is True and msgs[0]["hint"] == "/a\n" + content and _pt(msgs[0]) <= MAX_PLAINTEXT
    _, msgs = t.add_perm(SID, "Write", {"file_path": "/a", "content": content * 2}, 0)
    m = msgs[0]
    assert m["full"] is False and m["hint"].endswith("…") and ("/a\n" + content * 2).startswith(m["hint"][:-1])
    assert MAX_PLAINTEXT - 3 < _pt(m) <= MAX_PLAINTEXT
    _, msgs = t.add_perm(SID, "T" * 3000, {}, 0)
    assert msgs[0]["full"] is False and msgs[0]["tool"] == "T" * 3000


def test_state_is_derived_from_pending_requests():
    t = SessionTable()
    t.upsert(SID, "/x", "", "running", 0)
    state = lambda: t.sessions_msg()["s"][0]["state"]  # noqa: E731
    preq, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    assert state() == "perm" and t.status()[0]["state"] == "perm"
    areq, _ = t.add_ask(SID, QS)
    assert state() == "ask"
    t.resolve(areq, "terminal")
    assert state() == "perm"
    t.resolve(preq, "terminal")
    assert state() == "running"


def test_perm_result_states():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    preq, _ = t.add_perm(SID, "Bash", {"command": "ls"}, 0)
    areq, _ = t.add_ask(SID, QS)
    assert t.perm_result(preq) is None
    for bad in (areq, "unknown"):
        try:
            t.perm_result(bad)
        except KeyError:
            continue
        raise AssertionError(bad)
    t.resolve(preq, "terminal")
    assert t.perm_result(preq) == {"released": True}
    assert t.handle_device({"t": "perm_reply", "req": preq, "decision": "allow"}) == []
    assert t.perm_result(preq) == {"released": True}


def test_perm_released_on_delete_and_ttl():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    t.upsert(sid(2), "/x", "", "idle", 0)
    a, _ = t.add_perm(SID, "Bash", {"command": "a"}, 0)
    b, _ = t.add_perm(sid(2), "Bash", {"command": "b"}, 0)
    t.delete(SID)
    t.expire(TTL + 1)
    assert t.perm_result(a) == t.perm_result(b) == {"released": True}


def test_tool_done_resolves_matching_perm_by_canonical_input():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    t.upsert(sid(2), "/x", "", "idle", 0)
    inp = {"file_path": "/a", "content": "x", "nested": {"b": 1, "a": [1, 2]}}
    other_sid, _ = t.add_perm(sid(2), "Write", inp, 0)
    other_tool, _ = t.add_perm(SID, "Edit", inp, 0)
    other_input, _ = t.add_perm(SID, "Write", {**inp, "content": "y"}, 0)
    first, _ = t.add_perm(SID, "Write", inp, 0)
    second, _ = t.add_perm(SID, "Write", inp, 0)
    reordered = {"nested": {"a": [1, 2], "b": 1}, "content": "x", "file_path": "/a"}
    assert t.tool_done(SID, "Write", reordered) == [{"t": "resolved", "req": first, "by": "terminal"}]
    assert t.perm_result(first) == {"released": True}
    for r in (other_sid, other_tool, other_input, second):
        assert t.perm_result(r) is None
    assert t.tool_done("unknown", "Write", inp) == []


def test_large_ask_is_shrunk_to_fit_plaintext_limit():
    import json
    from cardbuddy import crypto
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    qs = [{"q": "質" * 200, "h": "見" * 20, "o": ["選" * 60] * 4, "m": True}] * 4
    req, msgs = t.add_ask(SID, qs)
    pt = json.dumps(msgs[0], ensure_ascii=False, separators=(",", ":")).encode()
    assert len(pt) <= crypto.MAX_PLAINTEXT
    q0 = msgs[0]["qs"][0]
    assert len(q0["q"]) == 30 and len(q0["o"][0]) == 10 and len(q0["h"]) == 12
    small = [{"q": "質" * 200, "h": "h", "o": ["選" * 60, "b"], "m": False}]
    _, msgs = t.add_ask(SID, small)
    assert len(msgs[0]["qs"][0]["q"]) == 120 and len(msgs[0]["qs"][0]["o"][0]) == 40
    assert t.handle_device({"t": "ask_reply", "req": req, "answers": [[0]] * 4}) != []


def _log_msg(t, n, sid8, p=None):
    msg = {"t": "log_req", "n": n, "id": sid8}
    if p is not None:
        msg["p"] = p
    return t.handle_device(msg)


def _page(t, p):
    return _log_msg(t, 1, SID[:8], p)[0]


def _log_table():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    return t


def test_session_log_keeps_newest_20_and_truncates():
    t = _log_table()
    t.add_log("unknown", "user", "ignored")
    for i in range(25):
        t.add_log(SID, "user" if i % 2 == 0 else "assistant", f"m{i}")
    t.add_log(SID, "assistant", "")
    assert _log_msg(t, 1, SID[:8]) == [{"t": "log", "n": 1, "p": 0, "more": False, "items": [
        {"r": "u" if i % 2 == 0 else "a", "x": f"m{i}", "c": False} for i in range(5, 25)]}]
    assert _page(t, 1) == {"t": "log", "n": 1, "p": 1, "more": False, "items": []}
    t.add_log(SID, "user", "y" * 5000)
    assert t.logs[SID][-1]["x"] == "y" * 3999 + "…"


def test_long_item_is_split_into_500_char_fragments():
    t = _log_table()
    t.add_log(SID, "assistant", "a" * 500 + "b" * 500 + "c" * 200)
    assert _page(t, 0)["items"] == [{"r": "a", "x": "a" * 500, "c": False},
                                    {"r": "a", "x": "b" * 500, "c": True},
                                    {"r": "a", "x": "c" * 200, "c": True}]


def test_pages_are_built_from_newest_and_cover_all_fragments():
    import json
    from cardbuddy import crypto
    t = _log_table()
    for i in range(10):
        t.add_log(SID, "user", f"u{i}")
        t.add_log(SID, "assistant", f"{i}" + "あ" * 799)
    pages = []
    while True:
        page = _page(t, len(pages))
        assert page["p"] == len(pages) and page["items"]
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= crypto.MAX_PLAINTEXT
        pages.append(page)
        if not page["more"]:
            break
    assert len(pages) > 2
    frags = [it for page in reversed(pages) for it in page["items"]]
    expected = []
    for it in t.logs[SID]:
        x = it["x"]
        expected += [{"r": it["r"], "x": x[i:i + 500], "c": i > 0} for i in range(0, len(x), 500)]
    assert frags == expected
    assert pages[0]["items"][-1] == {"r": "a", "x": "あ" * 300, "c": True}
    assert _page(t, len(pages)) == {"t": "log", "n": 1, "p": len(pages), "more": False, "items": []}


def test_page_boundary_keeps_continuation_flag():
    t = _log_table()
    t.add_log(SID, "assistant", "あ" * 1200)
    p0, p1, p2 = _page(t, 0), _page(t, 1), _page(t, 2)
    assert (p0["items"], p0["more"]) == ([{"r": "a", "x": "あ" * 200, "c": True}], True)
    assert (p1["items"], p1["more"]) == ([{"r": "a", "x": "あ" * 500, "c": True}], True)
    assert (p2["items"], p2["more"]) == ([{"r": "a", "x": "あ" * 500, "c": False}], False)


def test_oversized_fragment_is_halved():
    t = _log_table()
    t.add_log(SID, "assistant", "\x01" * 500)
    p0, p1 = _page(t, 0), _page(t, 1)
    assert (p0["items"], p0["more"]) == ([{"r": "a", "x": "\x01" * 250, "c": True}], True)
    assert (p1["items"], p1["more"]) == ([{"r": "a", "x": "\x01" * 250, "c": False}], False)


def test_log_req_bad_page_is_dropped():
    t = _log_table()
    t.add_log(SID, "user", "hi")
    for bad in (-1, "1", True, 1.5, None):
        msg = {"t": "log_req", "n": 1, "id": SID[:8], "p": bad}
        assert t.handle_device(msg) == [], bad


def test_log_req_requires_n_and_id_match():
    t = _log_table()
    t.add_log(SID, "user", "hi")
    assert _log_msg(t, 1, "deadbeef") == [{"t": "log", "n": 1, "p": 0, "more": False, "items": []}]
    assert _log_msg(t, 2, SID[:8], 3) == [{"t": "log", "n": 2, "p": 3, "more": False, "items": []}]
    assert _log_msg(t, True, SID[:8]) == []
    assert _log_msg(t, 1, 5) == []
    t.delete(SID)
    assert SID not in t.logs


def test_sessions_last_is_first_line_of_newest_answer():
    t = _log_table()
    last = lambda: t.sessions_msg()["s"][0]["last"]  # noqa: E731
    assert last() == ""
    t.add_log(SID, "assistant", "答えの1行目\n2行目")
    t.add_log(SID, "user", "次の質問")
    assert last() == "答えの1行目"
    t.add_log(SID, "assistant", "x" * 50)
    assert last() == "x" * 39 + "…"


def test_sessions_are_shrunk_to_fit_plaintext():
    import json
    from cardbuddy import crypto
    t = SessionTable()
    for i in range(9):
        s = f"{i}" + "-" * 40
        t.upsert(s, "/w/" + "名" * 20, "題" * 30, "idle", 0)
        t.add_log(s, "assistant", "返" * 60)
    msg = t.sessions_msg()
    assert len(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode()) <= crypto.MAX_PLAINTEXT
    assert {(len(r["title"]), len(r["last"])) for r in msg["s"]} == {(12, 20)}
    t2 = SessionTable()
    t2.upsert(SID, "/w/p", "題" * 30, "idle", 0)
    t2.add_log(SID, "assistant", "返" * 60)
    r = t2.sessions_msg()["s"][0]
    assert (len(r["title"]), len(r["last"])) == (24, 40)


def test_abandoned_perm_is_aborted_after_idle_limit():
    from cardbuddy.session_table import PERM_IDLE
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    a, _ = t.add_perm(SID, "Bash", {"command": "a"}, 0)
    b, _ = t.add_perm(SID, "Bash", {"command": "b"}, 0)
    c, _ = t.add_perm(SID, "Bash", {"command": "c"}, 50)
    t.perm_wait_begin(b)
    t.upsert(SID, "/x", "", "idle", PERM_IDLE)
    assert t.expire(PERM_IDLE) == []
    t.upsert(SID, "/x", "", "idle", PERM_IDLE + 1)
    assert t.expire(PERM_IDLE + 1) == [{"t": "resolved", "req": a, "by": "abort"}]
    assert t.perm_result(a) == {"released": True}
    assert t.perm_result(b) is None and t.perm_result(c) is None
    t.perm_wait_end(b, 100)
    t.upsert(SID, "/x", "", "idle", 111)
    assert t.expire(111) == [{"t": "resolved", "req": c, "by": "abort"}]
    t.perm_wait_begin(b)
    t.perm_wait_begin(b)
    t.perm_wait_end(b, 105)
    t.upsert(SID, "/x", "", "idle", 1000)
    assert t.expire(1000) == []
    t.perm_wait_end(b, 1000)
    t.upsert(SID, "/x", "", "idle", 1061)
    assert t.expire(1061) == [{"t": "resolved", "req": b, "by": "abort"}]
    for req in (a, "unknown"):
        t.perm_wait_begin(req)
        t.perm_wait_end(req, 0)


def test_ask_is_not_subject_to_perm_idle_limit():
    t = SessionTable()
    t.upsert(SID, "/x", "", "idle", 0)
    t.add_ask(SID, QS)
    t.upsert(SID, "/x", "", "idle", 1000)
    assert t.expire(1000) == []
