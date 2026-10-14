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


def test_pending_requests_stand_out():
    out = json.loads(subprocess.run(["node", str(Path(__file__).parent / "js" / "pending_harness.js")],
                                    capture_output=True, text=True, check=True).stdout)
    assert out["none"] == {"bar": False, "title": "CardBuddy"}
    assert out["list"]["bar"] and "要対応 3 件" in out["list"]["barText"] and out["list"]["title"] == "(3) CardBuddy"
    assert out["list"]["order"] == ["aaaaaaaa-1", "bbbbbbbb-2", "cccccccc-3"]
    assert out["list"]["attention"] == [True, True, False]
    assert out["items"]["open"] and out["items"]["reqs"] == ["r3", "r2", "r1"]
    assert "#2 beta" in out["items"]["text"] and "どれにする？" in out["items"]["text"] and "Edit" in out["items"]["text"]
    assert out["jump"] == {"current": "bbbbbbbb-2", "cards": ["r2"], "listOpen": False}
    assert out["detail"]["cards"] == ["r3", "r1"] and "このセッション 2 件" in out["detail"]["barText"]
    assert "他のセッション: 1 件" in out["detail"]["barText"]
    assert "他のセッション: 2 件" in out["other"]
    assert out["flash"] and out["guarded"] == 0 and out["allowed"] == 1
    assert out["badge"] == 5 and out["title"] == "(5) CardBuddy"


def test_pending_cards_come_first_in_session_view():
    html = (UI / "index.html").read_text()
    detail = html[html.index('id="detail"'):]
    assert detail.index('id="d-reqs"') < detail.index('id="activity"') < detail.index('id="log"')
    header = html[html.index("<header>"):html.index("</header>")]
    assert 'id="pending-bar"' in header and 'id="pending-list"' in header
