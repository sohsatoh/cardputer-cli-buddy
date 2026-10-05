"""ローマ字かな変換（かなのみ、漢字変換はしない）。

実機の空きメモリが少ないので、変換表は dict にせず、短い文字列から計算で引く。
規則は一般的な IME に合わせる（子音の重ねで「っ」、n の後に母音や y が来なければ「ん」、nn / n' で「ん」）。
"""

_V = "aiueo"
# 英字 1 文字の子音 + あいうえお段の 5 文字、? の所は _EXTRA で引く
_ROWS = "kかきくけこgがぎぐげごsさしすせそzざじずぜぞtたちつてとdだぢづでどnなにぬねのhはひふへほ" \
    "bばびぶべぼpぱぴぷぺぽmまみむめもrらりるれろcかしくせこyやいゆ?よwわ?う?を"
_YOUON = "kgsztdnhbpmr"
_SMALL_Y = "ゃぃゅぇょ"
_SMALL_A = "ぁぃぅぇぉ"
# 子音部=基のかな + 付ける小書き（Y: ゃぃゅぇょ / A: ぁぃぅぇぉ）+ 基だけになる母音
_SPECIAL = " sh=しYi ch=ちYi j=じYi jy=じY cy=ちY ts=つAu f=ふAu fy=ふY v=ゔAu vy=ゔY wh=うAu" \
    " q=くAu th=てY dh=でY tw=とA dw=どA kw=くA gw=ぐA "
_EXTRA = " ye=いぇ wi=うぃ we=うぇ xtu=っ ltu=っ xtsu=っ ltsu=っ xwa=ゎ lwa=ゎ xka=ゕ lka=ゕ xke=ゖ lke=ゖ" \
    " nn=ん n'=ん "
# 打鍵の途中でありうる子音部、前方一致で「まだ確定できない」を判定する
_CONS = " k g s z t d n h b p m r c y w q f v j x l ky gy sy zy ty dy ny hy by py my ry cy jy fy vy" \
    " xy ly sh ch ts th dh wh tw dw kw gw xt lt xts lts xw lw xk lk n' "
_PUNCT = "-ー,、.。[「]」"


def _find(table, key):
    i = table.find(" " + key + "=")
    return -1 if i < 0 else i + len(key) + 2


def lookup(r):
    """ローマ字 r がちょうどかなに当たれば、そのかなを返す。"""
    i = _find(_EXTRA, r)
    if i >= 0:
        return _EXTRA[i : _EXTRA.index(" ", i)]
    if not r or r[-1] not in _V:
        return None
    v = _V.index(r[-1])
    c = r[:-1]
    if not c:
        return "あいうえお"[v]
    if c in ("x", "l"):
        return _SMALL_A[v]
    if c in ("xy", "ly"):
        return _SMALL_Y[v]
    if len(c) == 1 and "a" <= c <= "z":
        i = _ROWS.find(c)
        if i >= 0 and _ROWS[i + 1 + v] != "?":
            return _ROWS[i + 1 + v]
    if len(c) == 2 and c[1] == "y" and c[0] in _YOUON:
        return _ROWS[_ROWS.index(c[0]) + 2] + _SMALL_Y[v]
    i = _find(_SPECIAL, c)
    if i >= 0:
        base, kind, only = _SPECIAL[i], _SPECIAL[i + 1], _SPECIAL[i + 2]
        if r[-1] == only:
            return base
        return base + (_SMALL_Y if kind == "Y" else _SMALL_A)[v]
    return None


class Romaji:
    """1 文字ずつ受け取り、確定したかなを返す。未確定のローマ字は pending に残る。"""

    def __init__(self):
        self.pending = ""

    def clear(self):
        self.pending = ""

    def back(self):
        self.pending = self.pending[:-1]

    def flush(self):
        """未確定分を確定する。n だけなら「ん」、それ以外はローマ字のまま。"""
        p, self.pending = self.pending, ""
        return "ん" if p == "n" else p

    def feed(self, ch):
        ch = ch.lower()
        if not ("a" <= ch <= "z" or (ch == "'" and self.pending == "n")):
            out = self.flush()
            i = _PUNCT.find(ch)
            return out + (_PUNCT[i + 1] if i >= 0 and i % 2 == 0 else ch)
        buf = self.pending + ch
        out = ""
        while buf:
            k = lookup(buf)
            if k is not None:
                self.pending = ""
                return out + k
            if (" " + buf) in _CONS:
                break
            a = buf[0]
            if len(buf) >= 2 and (a == buf[1] and a not in _V and a != "n" or buf[:2] == "tc"):
                out += "っ"
            elif a == "n":
                out += "ん"
            else:
                out += a
            buf = buf[1:]
        self.pending = buf
        return out


def to_kata(s):
    return "".join(chr(ord(c) + 0x60) if "ぁ" <= c <= "ゖ" else c for c in s)
