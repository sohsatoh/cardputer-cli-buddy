"""buddy_ui_cp のキー操作を、M5 の偽物と本物の Protocol で検証する。"""

import sys
import time
import types

import pytest

from test_device_protocol import Dev


FONT_H = 16  # EFontJA24 を 0.6 倍にしたときの fontHeight（0.5 倍の実測 13 から見積もり）
HINT_Y = 119


class FakeLcd:
    FONTS = types.SimpleNamespace(DejaVu9=None, EFontJA24=None)

    def __init__(self):
        self.drawn = []
        self.pos = []

    def textWidth(self, s):
        return sum(13 if ord(c) > 0x7E else 7 for c in s)  # 0.5 倍の実測（6 / 11）を 0.6 倍にした値

    def drawString(self, s, x, y):
        self.drawn.append(s)
        self.pos.append((s, x, y))

    def __getattr__(self, name):
        return lambda *a, **kw: None


@pytest.fixture
def env(monkeypatch):
    clock = [0]
    monkeypatch.setitem(sys.modules, "M5", types.SimpleNamespace(Lcd=FakeLcd()))
    monkeypatch.setattr(time, "ticks_ms", lambda: clock[0], raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.delitem(sys.modules, "buddy_ui_cp", raising=False)
    import buddy_ui_cp

    d = Dev()
    d.handshake()
    ui = buddy_ui_cp.BuddyUI(d.p)
    ui.set_connection("connected")

    def keys(*ks):
        for k in ks:
            clock[0] += 50
            ui.on_key(k)
            ui.refresh()

    def wait():
        ui.refresh()
        clock[0] += 1000
        ui.refresh()

    d.push({"t": "sessions", "s": [
        {"n": 1, "id": "aaaa1111", "name": "a", "title": "実装", "state": "idle"},
        {"n": 3, "id": "cccc3333", "name": "c", "title": "t", "state": "perm"},
    ]})
    ui.refresh()
    return d, ui, keys, wait


def test_perm_needs_settle_time_then_y(env):
    d, ui, keys, wait = env
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "hint": "ls"})
    ui.refresh()
    keys(ord("y"))
    assert d.sent == []
    wait()
    keys(ord("y"))
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "allow"}]
    assert d.p.queue == []


def test_perm_shows_session_context_and_desc(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "desc": "Remove build dir", "hint": "rm -rf ./build"})
    lcd.drawn.clear()
    ui.refresh()
    text = "\n".join(lcd.drawn)
    assert "#3 c" in text
    assert "Bash: Remove build dir" in text
    assert "$ rm -rf ./build" in text


def test_perm_long_hint_scrolls(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    hint = " ".join("arg%02d" % i for i in range(40))
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "desc": "", "hint": hint})
    wait()
    assert "arg39" not in "".join(lcd.drawn[-8:])
    keys(*[ord(".")] * 10)
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert any("arg39" in s for s in lcd.drawn) and any("続き" in s for s in lcd.drawn)
    keys(ord("y"))
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "allow"}]


def test_perm_not_full_accepts_only_deny(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    hint = "/src/app.py\n- " + "\n- ".join("old%d" % i for i in range(8)) + "\n+ new_last_line"
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "n": 3, "req": "p1", "tool": "Edit", "desc": "", "hint": hint, "full": False})
    wait()
    text = "\n".join(lcd.drawn)
    assert "端末で確認" in text
    hints = [s for s, x, y in lcd.pos if y == HINT_Y]
    assert hints and "N 拒否" in hints[-1] and "Y" not in hints[-1]
    keys(ord("y"), ord("Y"))
    assert d.sent == [] and [q["req"] for q in d.p.queue] == ["p1"]
    keys(*[ord(".")] * 20)
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert "+ new_last_line" in lcd.drawn  # 改行を含む全文を最後まで読める
    _assert_on_screen(lcd)
    keys(ord("n"))
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "deny"}]


def test_perm_without_full_field_is_not_allowed(env):
    d, ui, keys, wait = env
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "n": 3, "req": "p1", "tool": "Bash", "desc": "", "hint": "ls"})
    wait()
    keys(ord("y"))
    assert d.sent == []


def test_perm_and_ask_show_name_from_message(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    d.push({"t": "perm", "id": "eeee5555", "name": "別プロジェクト", "n": 3, "req": "p1", "tool": "Bash", "desc": "", "hint": "ls", "full": True})
    lcd.drawn.clear()
    ui.refresh()
    text = "\n".join(lcd.drawn)
    assert "#3 別プロジェクト" in text and "#3 c" not in text
    d.push({"t": "resolved", "req": "p1", "by": "terminal"})
    qs = [{"q": "どれ?", "h": "H", "o": ["a", "b"], "m": False}]
    d.push({"t": "ask", "id": "ffff7777", "name": "未登録の名前", "n": 7, "req": "a1", "qs": qs})
    lcd.drawn.clear()
    ui.refresh()
    assert "未登録の名前" in "\n".join(lcd.drawn)


def test_settle_counts_from_when_perm_is_shown(env):
    d, ui, keys, wait = env
    keys(ord("3"), ord("l"))
    assert d.replies()[0]["t"] == "log_req"
    keys(10, *b"hi")
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "hint": "ls"})
    wait()  # 入力中は perm を表示しないので、ここで待っても数えない
    keys(96)
    assert ui.mode == "log"
    keys(ord("y"))
    assert d.sent == []
    wait()
    keys(ord("y"))
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "allow"}]


def test_perm_deny_and_queue_order(env):
    d, ui, keys, wait = env
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "hint": "ls"})
    d.push({"t": "perm", "id": "aaaa1111", "name": "a", "full": True, "n": 1, "req": "p2", "tool": "Edit", "hint": "/x"})
    wait()
    keys("n")
    wait()
    keys("Y")
    assert d.replies() == [
        {"t": "perm_reply", "req": "p1", "decision": "deny"},
        {"t": "perm_reply", "req": "p2", "decision": "allow"},
    ]


def test_typing_is_not_an_answer(env):
    d, ui, keys, wait = env
    keys(ord("3"), 0x0A)
    assert ui.mode == "input"
    keys(*b"hi")
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "hint": "ls"})
    wait()
    keys(ord("y"), 0x08, ord("!"), 0x0A)
    assert d.replies() == [{"t": "prompt", "n": 3, "id": "cccc3333", "text": "hi!"}]
    assert [q["req"] for q in d.p.queue] == ["p1"]
    d.push({"t": "ack_prompt", "n": 3, "ok": True, "queued": True})
    ui.refresh()
    assert "キュー済み" in ui.status[0]
    assert any("キュー済み" in x for x in _redraw(ui))  # ヘッダの左に出る
    wait()
    wait()
    wait()
    wait()
    wait()
    assert ui.status[0] == "" and "CLI Buddy" in _redraw(ui)


def test_escape_cancels_input(env):
    d, ui, keys, wait = env
    keys(0x0A, *b"abc", 0x1B)
    assert ui.mode == "list" and d.sent == []
    keys(0x0A, ord("x"), ord("`"))
    assert ui.mode == "list" and d.sent == []


def test_ask_single_then_multi(env):
    d, ui, keys, wait = env
    qs = [
        {"q": "which?", "h": "H", "o": ["a", "b"], "m": False},
        {"q": "many?", "h": "M", "o": ["a", "b", "c"], "m": True},
    ]
    d.push({"t": "ask", "n": 1, "req": "a1", "qs": qs})
    wait()
    keys(ord("."), 0x0A)  # 下へ移動して確定 → [1]
    wait()
    keys(0x0A)  # multi で何も選んでいないので確定しない
    keys(ord(" "), ord("3"), ord(" "), ord("2"), ord(" "), ord(" "), 0x0A)
    assert d.replies() == [{"t": "ask_reply", "req": "a1", "answers": [[1], [0, 2]]}]


def test_resolved_discards_partial_ask(env):
    d, ui, keys, wait = env
    qs = [{"q": "q1", "h": "", "o": ["a", "b"], "m": False}, {"q": "q2", "h": "", "o": ["a", "b"], "m": False}]
    d.push({"t": "ask", "n": 1, "req": "a1", "qs": qs})
    wait()
    keys(0x0A)
    d.push({"t": "resolved", "req": "a1", "by": "terminal"})
    d.push({"t": "ask", "n": 1, "req": "a2", "qs": qs})
    wait()
    keys(ord("2"), 0x0A)
    wait()
    keys(0x0A)
    assert d.replies() == [{"t": "ask_reply", "req": "a2", "answers": [[1], [0]]}]


def test_list_selection_and_quit(env):
    d, ui, keys, wait = env
    keys(ord("3"))
    assert ui.sel == 1
    keys(ord(";"))
    assert ui.sel == 0
    keys(ord("9"))
    assert ui.sel == 0
    assert ui.on_key(ord("q")) == "quit"


UP, DOWN, LEFT, RIGHT = 181, 182, 180, 183


def test_fn_arrows_move_in_list_and_ask(env):
    d, ui, keys, wait = env
    keys(DOWN)
    assert ui.sel == 1
    keys(UP)
    assert ui.sel == 0
    d.push({"t": "ask", "n": 1, "req": "a1", "qs": [{"q": "?", "h": "", "o": ["a", "b", "c"], "m": False}]})
    wait()
    keys(DOWN, DOWN, UP, 10)
    assert d.replies() == [{"t": "ask_reply", "req": "a1", "answers": [[1]]}]


def test_cursor_insert_delete_and_move(env):
    d, ui, keys, wait = env
    keys(10, *b"abc", LEFT, LEFT, ord("X"))
    assert (ui.text, ui.cur) == ("aXbc", 2)
    keys(8)
    assert (ui.text, ui.cur) == ("abc", 1)
    keys(LEFT, LEFT, 8)
    assert (ui.text, ui.cur) == ("abc", 0)
    keys(*[RIGHT] * 5, ord(";"), ord("."))
    assert (ui.text, ui.cur) == ("abc;.", 5)
    keys(10)
    assert d.replies()[0]["text"] == "abc;."


def test_cursor_moves_by_wrapped_line_and_scrolls(env):
    d, ui, keys, wait = env
    keys(10, *b"a" * 80)  # 1 行 32 文字（228px / 7px）で 3 行に折り返す
    assert ui.cur == 80
    keys(UP)
    assert ui.cur == 48
    keys(UP, UP)
    assert ui.cur == 16
    keys(DOWN)
    assert ui.cur == 48
    keys(*[RIGHT] * 100, *b"b" * 300)
    lcd = sys.modules["M5"].Lcd
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert any(s.endswith("b" * 10) for s in lcd.drawn)  # カーソルのある最終行が見えている
    keys(*[UP] * 20)
    assert ui.cur < 32
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert "a" * 32 in lcd.drawn


def _open_log(keys, d, key=ord("l")):
    keys(ord("3"), key)
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 0}]


def test_log_screen_shows_latest_and_scrolls(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    assert ui.mode == "log"
    items = [{"r": "u", "x": "最初の依頼"}] + [{"r": "a", "x": "返答 %d" % i} for i in range(20)] + [{"r": "u", "x": "最後"}]
    d.push({"t": "log", "n": 3, "p": 0, "more": False, "items": items})
    lcd = sys.modules["M5"].Lcd
    lcd.drawn.clear()
    ui.refresh()
    assert "> 最後" in lcd.drawn and "> 最初の依頼" not in lcd.drawn
    bottom = ui._log_top
    keys(ord(";"))
    assert ui._log_top < bottom
    keys(*[UP] * 30)
    assert ui._log_top == 0
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert "> 最初の依頼" in lcd.drawn
    keys(*[ord(".")] * 30)
    assert ui._log_top == bottom


def test_log_rerequest_on_r_and_state_change(env):
    d, ui, keys, wait = env
    _open_log(keys, d, key=RIGHT)
    keys(ord("r"))
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 0}]
    sessions = [
        {"n": 1, "id": "aaaa1111", "name": "a", "title": "実装", "state": "running"},
        {"n": 3, "id": "cccc3333", "name": "c", "title": "t", "state": "perm"},
    ]
    d.push({"t": "sessions", "s": sessions})
    ui.refresh()
    assert d.replies() == []  # 他のセッションの変化では再要求しない
    sessions[1]["state"] = "idle"
    d.push({"t": "sessions", "s": sessions})
    ui.refresh()
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 0}]


def test_log_ignores_other_session_and_navigates(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push({"t": "log", "n": 1, "p": 0, "more": False, "items": [{"r": "u", "x": "別セッション"}]})
    lcd = sys.modules["M5"].Lcd
    lcd.drawn.clear()
    ui.refresh()
    assert "> 別セッション" not in lcd.drawn
    keys(10, *b"hi", 96)
    assert ui.mode == "log"
    keys(10, *b"go", 10)
    assert d.replies() == [{"t": "prompt", "n": 3, "id": "cccc3333", "text": "go"}]
    assert ui.mode == "log"
    keys(96)
    assert ui.mode == "list"


def test_perm_interrupts_log_then_returns(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push({"t": "perm", "id": "cccc3333", "name": "c", "full": True, "n": 3, "req": "p1", "tool": "Bash", "hint": "ls"})
    wait()
    keys(ord("y"))
    assert d.replies() == [{"t": "perm_reply", "req": "p1", "decision": "allow"}]
    assert ui.mode == "log"


def _page(p, more, items):
    return {"t": "log", "n": 3, "p": p, "more": more, "items": items}


def _redraw(ui):
    lcd = sys.modules["M5"].Lcd
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    return lcd.drawn


def test_list_shows_two_rows_per_session(env):
    d, ui, keys, wait = env
    d.push({"t": "sessions", "s": [
        {"n": i, "id": "id%06d" % i, "name": "名前%d" % i, "title": "題%d" % i, "state": "idle",
         "last": "返答%d" % i if i != 2 else ""} for i in range(1, 10)
    ]})
    drawn = _redraw(ui)
    assert "1 名前1" in drawn and "返答1" in drawn and "題1" not in drawn
    assert "題2" in drawn  # last が空なら title
    assert "1 名前1" in drawn and "3 名前3" in drawn and "4 名前4" not in drawn  # 1 画面 3 件
    keys(ord("9"))
    drawn = _redraw(ui)
    assert "9 名前9" in drawn and "返答9" in drawn and "6 名前6" not in drawn


def test_log_pages_prepend_and_continue_fragments(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push(_page(0, True, [{"r": "a", "x": "新しい返答の続き", "c": True}] + [{"r": "a", "x": "行%d" % i} for i in range(10)]))
    drawn = _redraw(ui)
    assert "行9" in drawn and "新しい返答の続き" not in drawn  # 最新が見える位置
    keys(*[UP] * 10)
    assert ui._log_top == 0
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 1}]
    assert "読み込み中…" in _redraw(ui)
    keys(UP)
    assert d.replies() == []  # 要求中は重ねて要求しない
    d.push(_page(1, False, [{"r": "u", "x": "古い依頼"}, {"r": "u", "x": "の続き", "c": True}, {"r": "a", "x": "古い返答の前半"}]))
    drawn = _redraw(ui)
    assert ui._log_top == 2  # 表示位置を保ったまま、2 行を上に足す
    assert "新しい返答の続き" in drawn and "古い返答の前半" not in drawn
    keys(*[UP] * 10)
    drawn = _redraw(ui)
    assert "> 古い依頼の続き" in drawn and "読み込み中…" not in drawn
    assert d.replies() == []  # more が false なら要求しない


def test_log_keeps_at_most_four_pages(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push(_page(0, True, [{"r": "a", "x": "p0-%d" % i} for i in range(8)]))
    for p in range(1, 6):
        keys(*[UP] * 20)
        assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": p}]
        d.push(_page(p, True, [{"r": "a", "x": "p%d-%d" % (p, i)} for i in range(8)]))
        ui.refresh()
    assert [pg[0] for pg in ui._pages] == [5, 4, 3, 2]
    assert len(ui._log_lines) == 32
    keys(*[ord(".")] * 40)  # 下端で、捨てた新しい側のページを取り直す
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 1}]
    d.push(_page(1, True, [{"r": "a", "x": "p1-%d" % i} for i in range(8)]))
    ui.refresh()
    assert [pg[0] for pg in ui._pages] == [4, 3, 2, 1]
    assert ui._more is True


def test_log_page0_replaces_and_follows_latest(env):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push(_page(0, True, [{"r": "a", "x": "古%d" % i} for i in range(10)]))
    keys(*[UP] * 20)
    d.replies()
    d.push(_page(1, False, [{"r": "a", "x": "もっと古い"}]))
    ui.refresh()
    keys(ord("r"))
    assert d.replies() == [{"t": "log_req", "n": 3, "id": "cccc3333", "p": 0}]
    d.push(_page(0, False, [{"r": "a", "x": "新%d" % i} for i in range(10)]))
    drawn = _redraw(ui)
    assert [pg[0] for pg in ui._pages] == [0] and "新9" in drawn and "もっと古い" not in drawn
    assert ui._log_top == len(ui._log_lines) - 5


def test_log_wraps_once_per_page(env, monkeypatch):
    d, ui, keys, wait = env
    _open_log(keys, d)
    d.push(_page(0, False, [{"r": "a", "x": "長い返答" * 100}]))
    ui.refresh()
    import buddy_ui_cp

    calls = []
    real = buddy_ui_cp._spans
    monkeypatch.setattr(buddy_ui_cp, "_spans", lambda text, w: (len(text) > 100 and calls.append(1)) or real(text, w))
    keys(*[UP] * 5, *[DOWN] * 5)
    assert calls == []  # キー入力では本文を折り返し直さない（見出しの _fit だけ）


def _assert_on_screen(lcd):
    assert lcd.pos
    for s, x, y in lcd.pos:
        assert 0 <= x and x + lcd.textWidth(s) <= 240, (s, x)
        assert 0 <= y and y + FONT_H <= 135, (s, y)
        if y == HINT_Y:
            assert not s.endswith("…"), s  # キーのヒントは省略せずに収める


def test_every_screen_fits(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    long_ja = "とても長い日本語のタイトルが画面からはみ出さないことを確かめる" * 3
    d.push({"t": "sessions", "s": [
        {"n": i, "id": "id%06d" % i, "name": "名前%d" % i, "title": long_ja[:12], "state": "running", "last": long_ja[:20]}
        for i in range(1, 10)
    ]})
    screens = []

    def snap():
        lcd.pos.clear()
        ui._dirty = True
        ui.refresh()
        _assert_on_screen(lcd)
        screens.append(len(lcd.pos))

    snap()
    keys(ord("9"))
    snap()
    d.push({"t": "perm", "id": "id000009", "name": "名前9", "full": True, "n": 9, "req": "p1", "tool": "Bash", "desc": long_ja[:80], "hint": long_ja[:160]})
    d.push({"t": "perm", "id": "id000009", "name": "名前9", "full": True, "n": 9, "req": "p2", "tool": "Edit", "hint": "/x"})
    snap()
    wait()
    keys(ord("y"))
    wait()
    keys(ord("y"))
    assert d.p.queue == []
    qs = [{"q": long_ja[:120], "h": "見出しが長い場合の例", "o": [long_ja[:40]] * 4, "m": True}]
    d.push({"t": "ask", "n": 9, "req": "a1", "qs": qs})
    snap()
    d.push({"t": "resolved", "req": "a1", "by": "terminal"})
    keys(ord("l"))
    assert ui.mode == "log"
    d.push({"t": "log", "n": 9, "p": 0, "more": True, "items": [{"r": "u", "x": long_ja}, {"r": "a", "x": "一行目\n" + long_ja}]})
    snap()
    keys(10, *("x" * 400).encode())
    d.push({"t": "perm", "id": "id000009", "name": "名前9", "full": True, "n": 9, "req": "p3", "tool": "Bash", "hint": "ls"})
    snap()
    assert ui.mode == "input" and len(screens) == 6


def test_japanese_is_drawn_as_is(env):
    d, ui, keys, wait = env
    lcd = sys.modules["M5"].Lcd
    lcd.drawn.clear()
    ui._dirty = True
    ui.refresh()
    assert any("実装" in s for s in lcd.drawn)


TAB = 0x2B


def test_tab_cycles_input_modes_and_shows_indicator(env):
    d, ui, keys, wait = env
    keys(10)
    assert ui.ime == 0 and "A 0/500" in _redraw(ui)
    keys(TAB)
    assert ui.ime == 1 and "あ 0/500" in _redraw(ui)
    keys(TAB)
    assert ui.ime == 2 and "ア 0/500" in _redraw(ui)
    keys(TAB)
    assert ui.ime == 0


def test_hiragana_input_and_send(env):
    d, ui, keys, wait = env
    keys(10, TAB, *b"kyouhaiitenkidesune")
    assert ui.text == "きょうはいいてんきですね"
    keys(*b"nihon")
    assert ui.text.endswith("にほ") and ui._ro.pending == "n"
    keys(10)  # Enter は未確定の n を「ん」にしてから送る
    assert d.replies() == [{"t": "prompt", "n": 1, "id": "aaaa1111", "text": "きょうはいいてんきですねにほん"}]


def test_katakana_and_pending_display(env):
    d, ui, keys, wait = env
    keys(10, TAB, TAB, *b"ra-men", ord("k"))
    assert ui.text == "ラーメン" and ui._ro.pending == "k"
    drawn = _redraw(ui)
    assert "ラーメン" in drawn and "k" in drawn  # 未確定のローマ字は別の色で続けて描く
    keys(8)
    assert ui._ro.pending == "" and ui.text == "ラーメン"  # BS は未確定分を先に消す
    keys(8)
    assert ui.text == "ラーメ"


def test_kana_inserts_at_cursor_and_commits_before_moving(env):
    d, ui, keys, wait = env
    keys(10, *b"ab", LEFT, TAB, *b"ka")
    assert (ui.text, ui.cur) == ("aかb", 2)
    keys(ord("s"), RIGHT)  # 矢印の前に未確定分を確定する
    assert (ui.text, ui.cur, ui._ro.pending) == ("aかsb", 4, "")
    keys(LEFT, ord("n"), TAB)  # モード切替でも確定する（n は「ん」）
    assert (ui.text, ui.ime) == ("aかsんb", 2)


def test_escape_discards_pending_and_mode_persists(env):
    d, ui, keys, wait = env
    keys(10, TAB, ord("k"), 96)
    assert ui.mode == "list" and d.sent == []
    keys(10)
    assert ui.ime == 1 and ui._ro.pending == "" and ui.text == ""


def test_kana_respects_500_chars(env):
    d, ui, keys, wait = env
    keys(10, *b"x" * 499, TAB, *b"kya")
    assert len(ui.text) == 500 and ui.text.endswith("x" + "き")
