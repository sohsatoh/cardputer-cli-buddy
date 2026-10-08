import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from cardbuddy import transcript
from cardbuddy.session_table import SessionTable

SID = "11111111-2222-3333-4444-555555555555"


def user(content, **kw):
    return {"type": "user", "isSidechain": False, "sessionId": SID, "message": {"role": "user", "content": content}, **kw}


def assistant(*blocks):
    return {"type": "assistant", "isSidechain": False, "sessionId": SID,
            "message": {"role": "assistant", "content": list(blocks)}}


SAMPLE = [
    {"type": "permission-mode", "permissionMode": "default"},
    user("架空のプロンプト", origin={"kind": "human"}),
    user("<local-command-caveat>内部</local-command-caveat>", isMeta=True),
    assistant({"type": "thinking", "thinking": "秘密の思考", "signature": "x"}),
    assistant({"type": "text", "text": "架空の返答"}),
    assistant({"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "echo hi", "description": "d"}}),
    user([{"type": "tool_result", "tool_use_id": "t1", "content": "hi\nthere", "is_error": False}]),
    assistant({"type": "tool_use", "id": "t2", "name": "Edit",
               "input": {"file_path": "/a/b.py", "old_string": "x", "new_string": "y"}}),
    user([{"type": "tool_result", "tool_use_id": "t2", "is_error": True,
           "content": [{"type": "text", "text": "失敗"}, {"type": "image", "source": {}}]}]),
    {"type": "system", "subtype": "x", "content": "内部"},
    {**assistant({"type": "text", "text": "サブエージェント"}), "isSidechain": True},
    user([{"type": "text", "text": "ブロックのプロンプト"}]),
    "not json",
]


@pytest.fixture
def env(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cb"))
    proj = d / ".claude" / "projects" / "-w-proj"
    proj.mkdir(parents=True)
    path = proj / f"{SID}.jsonl"
    path.write_text("".join((json.dumps(e, ensure_ascii=False) if isinstance(e, dict) else e) + "\n" for e in SAMPLE))
    monkeypatch.setenv("CARDBUDDY_TRANSCRIPTS", str(d / ".claude*" / "projects" / "*" / "{sid}.jsonl"))
    transcript.CACHE.clear()
    table = SessionTable()
    table.upsert(SID, "/w/proj", "", "running", 0)
    yield d, path, table
    shutil.rmtree(d)


def kinds(page):
    return [(it["line"], it["kind"]) for it in page["items"]]


def test_parse_kinds_and_skips_internal_entries(env):
    _, _, table = env
    page = transcript.read(table, SID)
    assert (page["start"], page["end"], page["total"]) == (0, len(SAMPLE), len(SAMPLE))
    assert kinds(page) == [(1, "user"), (4, "assistant"), (5, "tool_use"), (6, "tool_result"), (7, "tool_use"),
                           (8, "tool_result"), (11, "user")]
    items = page["items"]
    assert items[0]["text"] == "架空のプロンプト"
    assert items[2]["name"] == "Bash" and items[2]["summary"] == "echo hi" and '"description"' in items[2]["text"]
    assert items[3] == {"line": 6, "kind": "tool_result", "text": "hi\nthere", "is_error": False}
    assert items[4]["summary"] == "/a/b.py"
    assert items[5]["is_error"] is True and items[5]["text"] == "失敗\n[画像]"
    assert all("秘密" not in json.dumps(it, ensure_ascii=False) for it in items)


def test_long_entries_are_truncated(env):
    _, path, table = env
    with path.open("a") as f:
        f.write(json.dumps(assistant({"type": "text", "text": "a" * (transcript.ENTRY_MAX + 50)})) + "\n")
    item = transcript.read(table, SID, after=len(SAMPLE))["items"][0]
    assert item["text"] == "a" * transcript.ENTRY_MAX + "…（50 文字省略）"


def test_paging_backwards_and_appending(env):
    _, path, table = env
    page = transcript.read(table, SID, limit=4)
    assert (page["start"], page["end"]) == (len(SAMPLE) - 4, len(SAMPLE))
    older = transcript.read(table, SID, before=page["start"], limit=4)
    assert (older["start"], older["end"]) == (len(SAMPLE) - 8, len(SAMPLE) - 4)
    first = transcript.read(table, SID, before=2, limit=10)
    assert (first["start"], first["end"]) == (0, 2) and kinds(first) == [(1, "user")]
    assert transcript.read(table, SID, after=len(SAMPLE))["items"] == []
    with path.open("a") as f:
        f.write(json.dumps(user("追記")) + "\n")
        f.write(json.dumps(user("書きかけ"))[:10])
    new = transcript.read(table, SID, after=len(SAMPLE))
    assert (new["start"], new["end"], new["total"]) == (len(SAMPLE), len(SAMPLE) + 1, len(SAMPLE) + 1)
    assert [it["text"] for it in new["items"]] == ["追記"]
    with path.open("a") as f:
        f.write(json.dumps(user("書きかけ"))[10:] + "\n")
    assert [it["text"] for it in transcript.read(table, SID, after=new["end"])["items"]] == ["書きかけ"]


def test_rewritten_file_rebuilds_index(env):
    _, path, table = env
    transcript.read(table, SID)
    path.write_text(json.dumps(user("作り直し")) + "\n")
    page = transcript.read(table, SID)
    assert page["total"] == 1 and page["items"][0]["text"] == "作り直し"


def test_unregistered_or_unsafe_sid_is_refused(env):
    _, _, table = env
    assert transcript.read(table, "99999999-0000-0000-0000-000000000000") is None
    table.upsert("../../etc", "/w", "", "idle", 0)
    table.upsert("a*", "/w", "", "idle", 0)
    assert transcript.read(table, "../../etc") is None and transcript.read(table, "a*") is None


def test_symlinks_are_refused(env):
    d, path, table = env
    outside = d / "outside.jsonl"
    outside.write_text(json.dumps(user("外")) + "\n")
    other = "22222222-2222-3333-4444-555555555555"
    table.upsert(other, "/w", "", "idle", 0)
    (path.parent / f"{other}.jsonl").symlink_to(outside)
    assert transcript.read(table, other) is None
    third = "33333333-2222-3333-4444-555555555555"
    table.upsert(third, "/w", "", "idle", 0)
    ext = d / "ext"
    ext.mkdir()
    (ext / f"{third}.jsonl").write_text(json.dumps(user("外")) + "\n")
    os.symlink(ext, path.parent.parent / "-linked")
    assert transcript.read(table, third) is None


def test_symlinked_projects_root_is_allowed(env):
    d, _, table = env
    (d / ".claude-max").mkdir()
    os.symlink(d / ".claude" / "projects", d / ".claude-max" / "projects")
    assert transcript.read(table, SID)["total"] == len(SAMPLE)
