import json
import shutil
import subprocess
from pathlib import Path

import pytest

JS = Path(__file__).parents[1] / "cardbuddy" / "webui" / "activity.js"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def derive(events, now, state="running"):
    script = (f"const A = require({json.dumps(str(JS))});"
              f"const d = A.derive({json.dumps(events)}, {now});"
              f"console.log(JSON.stringify({{...d, state: A.state({json.dumps(state)}, d)}}));")
    out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


TURN = {"type": "turn_start", "turn_id": "t1", "prompt": "やって", "at": 100.0}
BASH = {"type": "tool_start", "tool_use_id": "u1", "tool": "Bash", "summary": "npm test", "at": 101.0}


def test_main_turn_in_progress():
    d = derive([TURN, BASH], 105)
    assert d["mainBusy"] and d["mainSince"] == 100 and d["prompt"] == "やって"
    assert [r["tool_use_id"] for r in d["running"]] == ["u1"] and d["state"] == "main"


def test_main_turn_finished_is_idle():
    d = derive([TURN, BASH, {"type": "tool_end", "tool_use_id": "u1", "is_error": False, "ms": 900, "at": 102.0},
                {"type": "turn_end", "turn_id": "t1", "reason": "answer", "ms": 3000, "at": 103.0}], 105)
    assert not d["mainBusy"] and d["running"] == [] and d["state"] == "idle"
    tool = next(r for r in d["rows"] if r["kind"] == "tool")
    assert tool["status"] == "ok" and tool["ms"] == 900 and not tool["sub"]


def test_background_subagent_after_main_turn_end():
    events = [TURN,
              {"type": "tool_start", "tool_use_id": "a0", "tool": "Agent", "at": 101.0},
              {"type": "tool_end", "tool_use_id": "a0", "is_error": False, "ms": 20, "at": 101.02},
              {"type": "turn_end", "turn_id": "t1", "reason": "answer", "ms": 1500, "at": 101.5},
              {"type": "tool_start", "tool_use_id": "s1", "tool": "Grep", "summary": None,
               "agent_id": "ag1", "agent_type": "Explore", "at": 110.0}]
    d = derive(events, 112)
    assert not d["mainBusy"] and d["subBusy"] and d["state"] == "sub"
    assert d["running"][0]["agent_type"] == "Explore"
    sub_row = next(r for r in d["rows"] if r.get("tool_use_id") == "s1")
    assert sub_row["sub"] and sub_row["status"] == "running"
    events.append({"type": "tool_end", "tool_use_id": "s1", "is_error": True, "ms": 5, "at": 115.0})
    assert derive(events, 120)["subBusy"]
    assert derive(events, 120)["rows"][-1]["status"] == "error"
    later = derive(events, 115 + 61)
    assert not later["subBusy"] and later["state"] == "idle"


def test_subagent_turn_end_without_agent_id_and_out_of_order():
    events = [TURN, {"type": "tool_end", "tool_use_id": "u1", "is_error": False, "ms": 1, "at": 101.5}, BASH,
              {"type": "turn_end", "turn_id": "t1", "reason": "answer", "ms": 2, "at": 102.0},
              {"type": "turn_end", "turn_id": "bg", "reason": "answer", "ms": 9000, "at": 104.0}]
    d = derive(events, 105)
    assert d["running"] == [] and not d["mainBusy"]
    ends = [r for r in d["rows"] if r["kind"] == "turn_end"]
    assert [r["sub"] for r in ends] == [False, True]


def test_pending_request_has_priority():
    assert derive([TURN, BASH], 105, state="perm")["state"] == "perm"
    assert derive([], 105, state="ask")["state"] == "ask"
    assert derive([], 105)["state"] == "idle"


def place(before, after):
    script = f"""
const A = require({json.dumps(str(JS))});
const moves = [];
const box = {{
  children: {json.dumps(before)},
  insertBefore(node, ref) {{
    moves.push(node);
    this.children = this.children.filter((x) => x !== node);
    const i = ref === null ? this.children.length : this.children.indexOf(ref);
    this.children.splice(i, 0, node);
  }},
}};
A.placeInOrder(box, {json.dumps(after)});
console.log(JSON.stringify({{children: box.children, moves}}));
"""
    return json.loads(subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout)


def test_place_in_order_does_not_touch_nodes_already_in_place():
    # 位置が変わらないノードを入れ直すと、iOS でフォーカス中の入力欄からキーボードが閉じる
    assert place(["a", "b", "c"], ["a", "b", "c"]) == {"children": ["a", "b", "c"], "moves": []}
    r = place(["a", "c"], ["a", "b", "c"])
    assert r == {"children": ["a", "b", "c"], "moves": ["b"]}
    assert place(["b", "a"], ["a", "b"])["children"] == ["a", "b"]


def test_app_uses_place_in_order_for_cards():
    app = (JS.parent / "app.js").read_text()
    body = app[app.index("function syncCards"):app.index("function render()")]
    assert "Activity.placeInOrder(container" in body and ".append(" not in body
