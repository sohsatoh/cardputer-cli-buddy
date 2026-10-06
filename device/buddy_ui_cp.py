"""Cardputer-Adv (240x135) の画面とキー操作。

画面は Protocol の状態（sessions / queue / last_ack / log）から毎回描き直す。キー入力は
on_key に渡す。perm / ask への返信は物理キーの押下でだけ送る。

描画の作法:
- `drawString` は左上基準。`setCursor` + `print` はベースライン基準でずれる。
- 日本語が出せるのは EFontJA24 だけなので、全画面をその 0.6 倍（高さ約 16px）で描き、行間は 16px。
- 幅はプロポーショナルなので、描画と同じフォント・倍率のまま `textWidth` で測る。
  フォントと倍率は __init__ で 1 回だけ設定し、ほかでは変えない。

### 画面

  y=0..16     ヘッダ（タイトルか状態表示と、接続状態）
  y=19..114   本体 6 行（一覧は 1 件 2 行 / ログ / perm / ask / 入力）
  y=119..134  キーのヒント
"""

import time

import M5

import kana

ORANGE = 0xCC785C
CREAM = 0xF0EEE6
DARK = 0x1F1F1F
BLACK = 0x000000
WHITE = 0xFFFFFF
GRAY_MID = 0x777777
GREEN = 0x00FF00
CYAN = 0x00FFFF
YELLOW = 0xFFFF00
RED = 0xFF0000

_LCD = M5.Lcd
_W = 240
_H = 135
_PAD = 6
_TW = _W - 2 * _PAD
_SIZE = 0.6
_FH = 16
_LH = 16
_Y0 = 19
_ROWS = 6
_HINT_Y = 119
_MAX_TEXT = 500
# 一覧で選んでいた指が、届いたばかりの perm / ask を確定してしまわないための猶予
_SETTLE_MS = 400
_SCROLL = 3
_STATUS_MS = 5000
BATTERY_MS = 20000  # 電池残量を読む間隔
BATTERY_LOW = 20  # これ以下は赤で出す
_MAX_PAGES = 4  # ログの保持ページ数（空きメモリ約 67KB に収めるため）

ENTER, ESC, BS = "ENTER", "ESC", "BS"
LEFT, RIGHT, UP, DOWN, TAB = "LEFT", "RIGHT", "UP", "DOWN", "TAB"
_TAB_CODE = 0x2B  # 実機の Tab は HID の usage 値 0x2B で届く（"+" と同じ値なので "+" は入力できない）
_IME = "Aあア"  # 入力モード: 英数 / ひらがな / カタカナ
_LANGS = ("ja-JP", "en-US")
# 矢印キーは単体だと ; , . / の文字になり、Fn と同時に押したときだけ 0xB4..0xB7 になる
_FN_ARROWS = {0xB4: LEFT, 0xB5: UP, 0xB6: DOWN, 0xB7: RIGHT}
_UPS = (";", ",", UP)
_DOWNS = (".", "/", DOWN)


def norm_key(k):
    """MatrixKeyboard.get_key() の値を、1 文字の str か ENTER などの名前にする。

    実機（UIFlow 2.5.3）では int で返る。Enter は 0x0A、Esc の位置のキーは ` (0x60)。
    """
    if isinstance(k, (bytes, bytearray)) and len(k) == 1:
        k = k[0]
    if isinstance(k, str) and len(k) == 1:
        k = ord(k)
    if not isinstance(k, int):
        return None
    if k in (0x0A, 0x0D):
        return ENTER
    if k in (0x1B, 0x60):
        return ESC
    if k in (0x08, 0x7F):
        return BS
    if k == _TAB_CODE:
        return TAB
    if k in _FN_ARROWS:
        return _FN_ARROWS[k]
    if 0x20 <= k <= 0x7E:
        return chr(k)
    return None


def _spans(text, w):
    """折り返した各行の (start, end) を返す。`\\n` でも改行する。"""
    out, start, x = [], 0, 0
    for i, ch in enumerate(text):
        if ch == "\n":
            out.append((start, i))
            start, x = i + 1, 0
            continue
        cw = _LCD.textWidth(ch)
        if x + cw > w and i > start:
            out.append((start, i))
            start, x = i, 0
        x += cw
    out.append((start, len(text)))
    return out


def _ell(text, w):
    while text and _LCD.textWidth(text + "…") > w:
        text = text[:-1]
    return text + "…"


def _fit(text, w=_TW):
    sp = _spans(text, w)
    if len(sp) == 1:
        return text
    return _ell(text[: sp[0][1]], w)


def _wrap(text, max_lines, w=_TW):
    sp = _spans(text, w)
    lines = [text[a:b] for a, b in sp[:max_lines]]
    if len(sp) > max_lines:
        lines[-1] = _ell(lines[-1], w)
    return lines


def _line_of(sp, c):
    li = 0
    for i, (a, _) in enumerate(sp):
        if a <= c:
            li = i
    return li


def _row(i):
    return _Y0 + _LH * i


def _text(s, x, y, color, bg=BLACK):
    _LCD.setTextColor(color, bg)
    _LCD.drawString(s, x, y)


def _right(s, y, color, bg=BLACK):
    _text(s, _W - _PAD - _LCD.textWidth(s), y, color, bg)


def _read_battery():
    """(残量 %, 充電中か) を返す。取れないか範囲外なら None。"""
    try:
        p = M5.Power
        level = p.getBatteryLevel()
        if not isinstance(level, int) or not 0 <= level <= 100:
            return None
        return level, p.isCharging() is True
    except Exception:
        return None


def _str(v):
    return v if isinstance(v, str) else str(v)


def _page_lines(items):
    """log 1 ページ分の items を、色付きの折り返し済みの行にする。c=true の断片は前の件につなぐ。"""
    texts = []
    for it in items:
        x = it["x"].replace("\t", " ")
        user = it.get("r") == "u"
        if it.get("c") and texts:
            texts[-1][0] += x
        else:
            texts.append([x if it.get("c") or not user else "> " + x, CYAN if user else CREAM])
    lines = []
    for x, color in texts:
        for a, b in _spans(x, _TW):
            lines.append((x[a:b], color))
    return lines


class BuddyUI:
    def __init__(self, proto, voice=None):
        self.p = proto
        self.voice = voice
        self.lang = _LANGS[0]
        self._vmsg = ("", GRAY_MID)  # 音声入力画面に出す結果やエラー
        self._vshown = None
        self._bat = None
        self._bat_ms = None
        self._in_ms = None
        self.conn = "advertising"
        self.mode = "list"  # list / log / input / voice（perm / ask は queue の先頭から決まる）
        self.sel = 0
        self.text = ""
        self.cur = 0
        self.target = None  # (n, id, name) 入力・ログの対象セッション
        self.status = ("", GRAY_MID)  # ヘッダの左に _STATUS_MS だけ出す
        self._status_seen = self.status
        self._status_ms = 0
        self._back = "list"
        self._in_top = 0
        self.ime = 0
        self._ro = kana.Romaji()
        self._log_state = None
        self._log_wait = None  # 要求中のページ (p, "reset" / "older" / "newer")
        self._pages = []  # [(p, lines)]、古いページが先
        self._more = False
        self._log_lines = []
        self._log_top = 0
        self._rev = None
        self._ack = None
        self._head = None
        self._head_ms = 0
        self._shown = None  # 画面に出ている perm / ask（入力中は出さない）
        self._perm_lines = (None, [])
        self._ask = None  # [qi, cur, picked, answers]
        self._perm_top = 0
        self._dirty = True
        _LCD.fillScreen(BLACK)
        try:
            _LCD.setFont(_LCD.FONTS.EFontJA24)
            _LCD.setTextSize(_SIZE)
        except Exception as e:
            print("buddy_ui_cp: setFont fallback:", e)
            _LCD.setFont(_LCD.FONTS.DejaVu9)
            _LCD.setTextSize(1)

    # ----- state

    def set_connection(self, state):
        if state != self.conn:
            self.conn = state
            self._dirty = True

    def _session(self, n, sid):
        for s in self.p.sessions:
            if s.get("n") == n and s.get("id") == sid:
                return s
        return None

    def _sync(self):
        q = self.p.queue
        head = q[0] if q else None
        if head is not self._head:
            self._head = head
            self._ask = [0, 0, [], []] if head and head["t"] == "ask" else None
            self._perm_top = 0
            self._dirty = True
        shown = head if self.mode != "input" else None
        if shown is not self._shown:
            self._shown = shown
            self._head_ms = time.ticks_ms()
        ack = self.p.last_ack
        if ack is not None and ack is not self._ack:
            n = ack.get("n")
            if not ack.get("ok"):
                self.status = ("#{} 失敗（セッションが無い）".format(n), RED)
            elif ack.get("queued"):
                self.status = ("#{} キュー済み".format(n), YELLOW)
            else:
                self.status = ("#{} 送信済み".format(n), GREEN)
        self._ack = ack
        now = time.ticks_ms()
        if self.status is not self._status_seen:
            self._status_seen, self._status_ms = self.status, now
        elif self.status[0] and time.ticks_diff(now, self._status_ms) >= _STATUS_MS:
            self.status = self._status_seen = ("", GRAY_MID)
            self._dirty = True
        if self.sel >= len(self.p.sessions):
            self.sel = max(0, len(self.p.sessions) - 1)
        if self.mode == "log":
            self._sync_log()
        self._sync_voice()

    def _sync_voice(self):
        v = self.voice
        st = v.state if v is not None else None
        if self._head is not None:
            # perm / ask の表示中に入力画面へ移ると、その画面で押したキーが本文や送信に回るので、結果は保留する
            if st in ("rec", "flush"):
                v.cancel()
                self._vmsg = ("割り込みのため取り消しました", YELLOW)
                self._dirty = True
            return
        res = self.p.take_voice()
        if v is None:
            return
        if res is not None and st == "wait" and res.get("vid") == v.vid:
            v.done()
            if res["t"] == "voice_text":
                self._open_input("list")
                self._insert(res["text"])
                self._in_ms = time.ticks_ms()
            else:
                self._vmsg = (res["err"], RED)
            self._dirty = True
        elif st == "lost":
            v.done()
            self._vmsg = (v.err or "取り消しました", RED)
            self._dirty = True
        if self.mode == "voice":
            # 録音中の描き直しは 1 回 40ms ほどかかり、送信と取り合うので 0.5 秒単位に間引く（実機で測定）
            shown = (v.state, v.elapsed_ms // 500, v.level >> 12, v.progress // 10 if v.state == "flush" else 0)
            if shown != self._vshown:
                self._vshown = shown
                self._dirty = True

    def _sync_log(self):
        n, sid, _ = self.target
        s = self._session(n, sid)
        state = s.get("state") if s else None
        if state != self._log_state:
            self._log_state = state
            self._req_log(0, "reset")
        log = self.p.take_log()
        if log is not None and log.get("n") == n:
            self._on_page(log)

    def _req_log(self, p, kind):
        if self.p.request_log(self.target[0], self.target[1], p):
            self._log_wait = (p, kind)

    def _log_bottom(self):
        return max(0, len(self._log_lines) - (_ROWS - 1))

    def _on_page(self, log):
        p = log.get("p", 0)
        wait = self._log_wait
        if p == 0:
            kind = wait[1] if wait and wait[0] == 0 else "reset"
        elif wait and wait[0] == p:
            kind = wait[1]
        else:
            print("buddy_ui_cp: drop unexpected log page", p)
            return
        self._log_wait = None
        more = log.get("more") is True
        lines = _page_lines(log["items"])
        pages = self._pages
        if p == 0:
            # ログが増えるとページ境界がずれるので、p=0 は持っているページを全部置き換える
            self._pages = [(0, lines)]
            self._more = more
        elif kind == "older":
            pages.insert(0, (p, lines))
            self._more = more
            self._log_top += len(lines)
            if len(pages) > _MAX_PAGES:
                pages.pop()
        else:
            pages.append((p, lines))
            if len(pages) > _MAX_PAGES:
                self._log_top -= len(pages.pop(0)[1])
                self._more = True
        self._log_lines = [ln for pg in self._pages for ln in pg[1]]
        bottom = self._log_bottom()
        if p == 0:
            self._log_top = 0 if kind == "newer" else bottom
        self._log_top = max(0, min(self._log_top, bottom))
        self._dirty = True

    def _settled(self):
        return time.ticks_diff(time.ticks_ms(), self._head_ms) >= _SETTLE_MS

    # ----- input

    def on_key(self, k):
        """1 キー分を処理する。アプリを終えるときは "quit" を返す。"""
        key = norm_key(k)
        if key is None:
            return None
        self._sync()
        self._dirty = True
        if self.mode == "input":
            self._key_input(key)
        elif self._head is not None and self._head["t"] == "perm":
            self._key_perm(key)
        elif self._head is not None:
            self._key_ask(key)
        elif self.mode == "log":
            self._key_log(key)
        elif self.mode == "voice":
            self._key_voice(key)
        else:
            return self._key_list(key)
        return None

    def _open_input(self, back):
        self._in_ms = None  # 音声の結果を入れたときだけ、Enter を _SETTLE_MS 受け付けない
        self.text, self.cur, self._in_top = "", 0, 0
        self._ro.clear()
        self._back = back
        self.mode = "input"

    def _key_list(self, key):
        ss = self.p.sessions
        if key == "q" or key == "Q":
            return "quit"
        if len(key) == 1 and "1" <= key <= "9":
            for i, s in enumerate(ss):
                if s.get("n") == int(key):
                    self.sel = i
        elif key in _UPS:
            self.sel = max(0, self.sel - 1)
        elif key in _DOWNS:
            self.sel = min(max(0, len(ss) - 1), self.sel + 1)
        elif key in ("v", "V") and self.voice is not None and self.p.ready and ss:
            s = ss[self.sel]
            self.target = (s.get("n"), s.get("id"), _str(s.get("name", "")))
            self._vmsg = ("", GRAY_MID)
            self.mode = "voice"
        elif key in (ENTER, "l", "L", RIGHT) and self.p.ready and ss:
            s = ss[self.sel]
            self.target = (s.get("n"), s.get("id"), _str(s.get("name", "")))
            if key == ENTER:
                self._open_input("list")
            else:
                self.p.take_log()
                self._pages, self._log_lines, self._more = [], [], False
                self._log_top, self._log_wait = 0, None
                self._log_state = s.get("state")
                self.mode = "log"
                self._req_log(0, "reset")
        return None

    def _key_log(self, key):
        if key == ESC:
            self.mode = "list"
        elif key == ENTER:
            self._open_input("log")
        elif key in ("r", "R"):
            self._req_log(0, "reset")
        elif key in _UPS:
            self._log_top = max(0, self._log_top - _SCROLL)
            if self._log_top == 0 and self._more and self._pages and self._log_wait is None:
                self._req_log(self._pages[0][0] + 1, "older")
        elif key in _DOWNS:
            bottom = self._log_bottom()
            self._log_top = min(bottom, self._log_top + _SCROLL)
            if self._log_top == bottom and self._pages and self._pages[-1][0] > 0 and self._log_wait is None:
                self._req_log(self._pages[-1][0] - 1, "newer")

    def _insert(self, s):
        t, c = self.text, self.cur
        s = s[: _MAX_TEXT - len(t)]
        self.text, self.cur = t[:c] + s + t[c:], c + len(s)

    def _kana(self, s):
        return kana.to_kata(s) if self.ime == 2 else s

    def _key_voice(self, key):
        v = self.voice
        st = v.state
        if key == ESC:
            v.cancel()
            self.mode = "list"
        elif key == TAB and st == "idle":
            self.lang = _LANGS[(_LANGS.index(self.lang) + 1) % len(_LANGS)]
        elif key in (ENTER, " "):
            if st == "rec":
                v.stop()
            elif st == "idle":
                self._vmsg = ("", GRAY_MID)
                if not v.start(self.lang):
                    self._vmsg = (v.err or "開始できません", RED)

    def _key_input(self, key):
        if key in (TAB, ENTER, LEFT, RIGHT, UP, DOWN):
            self._insert(self._kana(self._ro.flush()))
        t, c = self.text, self.cur
        if key == ESC:
            self._ro.clear()
            self.mode = self._back
            self.status = ("取り消しました", GRAY_MID)
        elif key == BS:
            if self._ro.pending:
                self._ro.back()
            elif c:
                self.text, self.cur = t[: c - 1] + t[c:], c - 1
        elif key == TAB:
            self.ime = (self.ime + 1) % len(_IME)
        elif key == LEFT:
            self.cur = max(0, c - 1)
        elif key == RIGHT:
            self.cur = min(len(t), c + 1)
        elif key in (UP, DOWN):
            sp = _spans(t, _TW)
            li = _line_of(sp, c)
            to = li - 1 if key == UP else li + 1
            if 0 <= to < len(sp):
                self.cur = min(sp[to][0] + c - sp[li][0], sp[to][1])
        elif key == ENTER:
            if not t.strip():
                return
            if self._in_ms is not None and time.ticks_diff(time.ticks_ms(), self._in_ms) < _SETTLE_MS:
                return
            n, sid, _ = self.target
            if self.p.send_prompt(n, sid, t):
                self.status = ("#{} 送信中…".format(n), CYAN)
            else:
                self.status = ("#{} 送信できません（未接続）".format(n), RED)
            self.mode = self._back
        elif len(key) == 1:
            self._insert(self._kana(self._ro.feed(key)) if self.ime else key)

    def _key_perm(self, key):
        if key in _UPS:
            self._perm_top = max(0, self._perm_top - _SCROLL)
            return
        if key in _DOWNS:
            self._perm_top += _SCROLL  # 上限は描画時に丸める
            return
        if not self._settled():
            return
        k = key.lower() if len(key) == 1 else key
        if k == "y" and self._head.get("full") is not True:
            return
        if k in ("y", "n"):
            if not self.p.reply_perm(self._head["req"], "allow" if k == "y" else "deny"):
                self.status = ("perm の返信に失敗", RED)

    def _key_ask(self, key):
        qs = self._head["qs"]
        st = self._ask
        q = qs[st[0]]
        n_opt = len(q["o"])
        if len(key) == 1 and "1" <= key <= "9" and int(key) <= n_opt:
            st[1] = int(key) - 1
        elif key in _UPS:
            st[1] = max(0, st[1] - 1)
        elif key in _DOWNS:
            st[1] = min(n_opt - 1, st[1] + 1)
        elif key == " " and q.get("m"):
            if st[1] in st[2]:
                st[2].remove(st[1])
            else:
                st[2].append(st[1])
        elif key == ENTER and self._settled():
            if q.get("m"):
                if not st[2]:
                    return
                st[3].append(sorted(st[2]))
            else:
                st[3].append([st[1]])
            st[0], st[1], st[2] = st[0] + 1, 0, []
            self._head_ms = time.ticks_ms()
            if st[0] == len(qs) and not self.p.reply_ask(self._head["req"], st[3]):
                self.status = ("ask の返信に失敗", RED)
                self._ask = [0, 0, [], []]

    # ----- drawing

    def _poll_battery(self):
        now = time.ticks_ms()
        if self._bat_ms is not None and time.ticks_diff(now, self._bat_ms) < BATTERY_MS:
            return
        self._bat_ms = now
        bat = _read_battery()
        if bat != self._bat:
            self._bat = bat
            self._dirty = True

    def refresh(self):
        self._poll_battery()
        self._sync()
        if self.p.rev != self._rev:
            self._rev = self.p.rev
            self._dirty = True
        if not self._dirty:
            return
        self._dirty = False
        self._draw_header()
        _LCD.fillRect(0, 18, _W, 99, BLACK)
        if not self.p.paired:
            hints = self._draw_unpaired()
        elif self.mode == "input":
            hints = self._draw_input()
        elif self._head is not None and self._head["t"] == "perm":
            hints = self._draw_perm()
        elif self._head is not None:
            hints = self._draw_ask()
        elif self.mode == "log":
            hints = self._draw_log()
        elif self.mode == "voice":
            hints = self._draw_voice()
        else:
            hints = self._draw_list()
        _LCD.fillRect(0, _HINT_Y - 2, _W, 1, ORANGE)
        _LCD.fillRect(0, _HINT_Y - 1, _W, _H - _HINT_Y + 1, DARK)
        _text(_fit(hints), _PAD, _HINT_Y, CREAM, DARK)

    def _draw_header(self):
        _LCD.fillRect(0, 0, _W, 17, DARK)
        _LCD.fillRect(0, 17, _W, 1, ORANGE)
        if not self.p.paired:
            label, color = "NO KEY", RED
        elif self.p.ready:
            label, color = getattr(self.p.link, "kind", "LINK"), GREEN
        elif self.conn == "connected":
            label, color = "HELLO..", YELLOW
        elif self.conn == "disconnected":
            label, color = "OFF", RED
        else:
            label, color = "ADV", CYAN
        n = len(self.p.queue)
        if n:
            label = "{}件待ち {}".format(n, label)
        right = _W - _PAD
        if self._bat is not None:
            level, charging = self._bat
            # 充電中の印は ⚡ が EFontJA24 で出るか確かめられないので、ASCII の + にする
            bat = ("+" if charging else "") + "{}%".format(level)
            right -= _LCD.textWidth(bat)
            _text(bat, right, 0, RED if level <= BATTERY_LOW else GRAY_MID, DARK)
            right -= _PAD
        right -= _LCD.textWidth(label)
        _text(label, right, 0, color, DARK)
        left, lc = self.status if self.status[0] else ("CLI Buddy", ORANGE)
        _text(_fit(left, right - _PAD - _PAD), _PAD, 0, lc, DARK)

    def _draw_unpaired(self):
        _text("未ペアリング", _PAD, _row(0), RED)
        _text(_fit("USB でつないで `buddy pair` を"), _PAD, _row(2), CREAM)
        _text(_fit("実行し、再起動してください"), _PAD, _row(3), CREAM)
        return "Q 終了"

    def _draw_list(self):
        ss = self.p.sessions
        if not self.p.ready:
            _text("buddyd を待っています…", _PAD, _row(0), CREAM)
        elif not ss:
            _text("セッションなし", _PAD, _row(0), GRAY_MID)
        per = _ROWS // 2
        top = min(max(0, self.sel - per + 1), max(0, len(ss) - per))
        for i in range(top, min(len(ss), top + per)):
            s = ss[i]
            y = _row(2 * (i - top))
            state = _str(s.get("state", ""))
            bg = DARK if i == self.sel else BLACK
            if i == self.sel:
                _LCD.fillRect(0, y - 1, _W, 2 * _LH, DARK)
            sw = _LCD.textWidth(state)
            head = "{} {}".format(s.get("n", "?"), _str(s.get("name", "")))
            _text(_fit(head, _TW - sw - _PAD), _PAD, y, CREAM, bg)
            _right(state, y, YELLOW if state in ("perm", "ask") else GRAY_MID, bg)
            sub = _str(s.get("last") or s.get("title", ""))
            _text(_fit(sub, _TW - 8), _PAD + 8, y + _LH, GRAY_MID, bg)
        return "Ent入力 lログ v音声 Q終了" if self.voice is not None else "1-9;.選択 Ent入力 lログ Q終了"

    def _draw_voice(self):
        n, _, name = self.target
        v = self.voice
        lw = _LCD.textWidth(self.lang)
        _text(_fit("音声 #{} {}".format(n, name), _TW - lw - _PAD), _PAD, _row(0), ORANGE)
        _right(self.lang, _row(0), GRAY_MID)
        st = v.state
        if st == "rec":
            _text("● 録音中 {} 秒".format(v.elapsed_ms // 1000), _PAD, _row(1), RED)
            y = _row(2) + 4
            _LCD.fillRect(_PAD, y, _TW, 8, DARK)
            _LCD.fillRect(_PAD, y, min(_TW, v.level * _TW // 12000), 8, GREEN)
            return "Ent:停止 Esc:取消"
        if st == "flush":
            _text("送信中 {}%".format(v.progress), _PAD, _row(1), CYAN)
            return "Esc:取消"
        if st == "wait":
            _text("認識中…", _PAD, _row(1), CYAN)
            return "Esc:取消"
        _text("Enter で録音を始めます", _PAD, _row(1), CREAM)
        if self._vmsg[0]:
            for i, line in enumerate(_wrap(self._vmsg[0], 3)):
                _text(line, _PAD, _row(3 + i), self._vmsg[1])
        return "Ent:録音 Tab:言語 Esc:戻る"

    def _draw_log(self):
        n, sid, name = self.target
        s = self._session(n, sid)
        state = _str(s.get("state", "")) if s else ""
        sw = _LCD.textWidth(state)
        _text(_fit("ログ #{} {}".format(n, name), _TW - sw - _PAD), _PAD, _row(0), ORANGE)
        _right(state, _row(0), GRAY_MID)
        hints = ";.送り r更新 Ent入力 Esc戻る"
        wait = self._log_wait
        if not self._pages or not self._log_lines:
            _text("読み込み中…" if wait else "ログなし", _PAD, _row(1), GRAY_MID)
            return hints
        first, rows = 1, _ROWS - 1
        if wait and wait[1] == "older" and self._log_top == 0:
            _text("読み込み中…", _PAD, _row(1), GRAY_MID)
            first, rows = 2, rows - 1
        elif wait and wait[1] == "newer":
            _text("読み込み中…", _PAD, _row(_ROWS - 1), GRAY_MID)
            rows -= 1
        top = self._log_top
        for i, (line, color) in enumerate(self._log_lines[top : top + rows]):
            _text(line, _PAD, _row(first + i), color)
        return hints

    def _draw_perm(self):
        m = self._head
        more = len(self.p.queue) - 1
        full = m.get("full") is True
        title = "PERM #{} {}".format(m.get("n", "?"), _str(m.get("name", "")))
        if more:
            title += "  (+{})".format(more)
        _text(_fit(title), _PAD, _row(0), ORANGE)
        first = 1
        if not full:
            _text(_fit("全文が長すぎます：端末で確認"), _PAD, _row(1), RED)
            first = 2
        if self._perm_lines[0] is not m:
            tool = _str(m.get("tool", ""))
            desc = _str(m.get("desc", ""))
            hint = _str(m.get("hint", ""))
            head = tool + (": " + desc if desc else "")
            body = "$ " + hint if tool == "Bash" else hint
            lines = [(head[a:b], WHITE) for a, b in _spans(head, _TW)]
            lines += [(body[a:b], CREAM) for a, b in _spans(body, _TW)]
            self._perm_lines = (m, lines)
        lines = self._perm_lines[1]
        rows = _ROWS - first
        last = max(0, len(lines) - rows)
        self._perm_top = min(self._perm_top, last)
        for i, (line, color) in enumerate(lines[self._perm_top : self._perm_top + rows]):
            _text(line, _PAD, _row(first + i), color)
        keys = "Y 許可  N 拒否" if full else "N 拒否"
        if last:
            return "{}  ;.続き {}/{}".format(keys, self._perm_top + 1, last + 1)
        return keys

    def _draw_ask(self):
        m = self._head
        qi, cur, picked, _ = self._ask
        qs = m["qs"]
        q = qs[qi]
        title = "ASK #{} {}  {}/{}  {}".format(m.get("n", "?"), _str(m.get("name", "")), qi + 1, len(qs), _str(q.get("h", "")))
        _text(_fit(title), _PAD, _row(0), ORANGE)
        opts = q["o"]
        q_rows = _ROWS - 1 - len(opts)
        for i, line in enumerate(_wrap(_str(q.get("q", "")), q_rows)):
            _text(line, _PAD, _row(1 + i), CREAM)
        multi = q.get("m")
        for i, o in enumerate(opts):
            mark = (">" if i == cur else " ") + ("[x]" if i in picked else "[ ]" if multi else "")
            line = "{} {}. {}".format(mark, i + 1, _str(o))
            _text(_fit(line), _PAD, _row(1 + q_rows + i), YELLOW if i == cur else WHITE)
        return "1-4;.選択 Spc切替 Ent確定" if multi else "1-4;.選択 Ent確定"

    def _draw_input(self):
        n, _, name = self.target
        count = "{} {}/{}".format(_IME[self.ime], len(self.text), _MAX_TEXT)
        cw = _LCD.textWidth(count)
        _text(_fit("→ #{} {}".format(n, name), _TW - cw - _PAD), _PAD, _row(0), ORANGE)
        _right(count, _row(0), GRAY_MID)
        first = 1
        if self.p.queue:
            _text(_fit("! {}件待ち（Esc で戻って回答）".format(len(self.p.queue))), _PAD, _row(1), RED)
            first = 2
        rows = _ROWS - first
        # 未確定のローマ字はカーソル位置に挟んで折り返し、色と下線で区別する
        pend = self._ro.pending
        c = self.cur
        text = self.text[:c] + pend + self.text[c:]
        pc, pe = c, c + len(pend)
        sp = _spans(text, _TW)
        li = _line_of(sp, pe)
        if li < self._in_top:
            self._in_top = li
        elif li >= self._in_top + rows:
            self._in_top = li - rows + 1
        self._in_top = min(self._in_top, max(0, len(sp) - rows))
        for i, (a, b) in enumerate(sp[self._in_top : self._in_top + rows]):
            y = _row(first + i)
            p0, p1 = min(max(pc, a), b), min(max(pe, a), b)
            x = _PAD
            for lo, hi, color in ((a, p0, WHITE), (p0, p1, CYAN), (p1, b, WHITE)):
                if lo < hi:
                    seg = text[lo:hi]
                    _text(seg, x, y, color)
                    w = _LCD.textWidth(seg)
                    if color == CYAN:
                        _LCD.fillRect(x, y + _FH - 1, w, 1, CYAN)
                    x += w
            if self._in_top + i == li:
                x = _PAD + _LCD.textWidth(text[a:pe])
                _LCD.fillRect(min(x, _W - 2), y, 2, _FH, ORANGE)
        return "Tab:かな Fn矢印:移動 Ent:送信"
