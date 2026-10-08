import json
import shutil
import subprocess
from pathlib import Path

import pytest

UI = Path(__file__).parents[1] / "cardbuddy" / "webui"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

LIST = {"list": True, "detail": False, "hlist": True, "hdetail": False}


def detail(title):
    return {"list": False, "detail": True, "hlist": False, "hdetail": True, "title": title}


def test_header_follows_the_current_view():
    out = json.loads(subprocess.run(["node", str(Path(__file__).parent / "js" / "view_harness.js")],
                                    capture_output=True, text=True, check=True).stdout)
    pick = lambda v, keys: {k: v[k] for k in keys}  # noqa: E731
    assert pick(out["start"], LIST) == LIST
    assert pick(out["openA"], detail("")) == detail("#1 alpha")
    assert pick(out["back"], LIST) == LIST and out["back"]["current"] is None
    assert pick(out["openB"], detail("")) == detail("#2 beta")
    assert pick(out["sseUpdate"], detail("")) == detail("#2 beta2")
    assert pick(out["popToA"], detail("")) == detail("#1 alpha")
    assert pick(out["popToList"], LIST) == LIST
    assert pick(out["pageshow"], detail("")) == detail("#1 alpha")
    assert pick(out["vanished"], LIST) == LIST
    assert "null" not in out["activityText"] and "待機中" in out["activityText"]


def test_hidden_attribute_wins_over_display_rules():
    # .hrow { display: flex } などが UA の [hidden] より強く、hidden を付けても表示されたままになっていた
    css = (UI / "app.css").read_text()
    assert "[hidden] { display: none !important; }" in css
