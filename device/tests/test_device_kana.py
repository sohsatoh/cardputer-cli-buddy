import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import kana  # noqa: E402


def conv(s, kata=False):
    r = kana.Romaji()
    out = "".join(r.feed(ch) for ch in s) + r.flush()
    return kana.to_kata(out) if kata else out


@pytest.mark.parametrize(
    "src, want",
    [
        ("kyouhaiitenkidesune", "きょうはいいてんきですね"),
        ("gakkou", "がっこう"),
        ("konnnichiha", "こんにちは"),
        ("shinbun", "しんぶん"),
        ("kan'i", "かんい"),
        ("xtu", "っ"),
        ("ltu", "っ"),
        ("xtsu", "っ"),
        ("ti", "ち"),
        ("chi", "ち"),
        ("fa", "ふぁ"),
        ("-", "ー"),
        ("ra-men", "らーめん"),
        ("a,i.", "あ、い。"),
        ("[kagi]", "「かぎ」"),
        ("matcha", "まっちゃ"),
        ("kitte", "きって"),
        ("onnna", "おんな"),  # nn で「ん」なので、な の前の ん は n を 3 つ打つ
        ("hon'ya", "ほんや"),
        ("honnya", "ほんや"),
        ("honya", "ほにゃ"),
        ("kanji", "かんじ"),
        ("sanpo", "さんぽ"),
        ("tenn", "てん"),
        ("n", "ん"),
        ("2024nen", "2024ねん"),
        ("abc!?", "あbc!?"),
        ("KYOU", "きょう"),
        ("dhi", "でぃ"),
        ("thi", "てぃ"),
        ("di", "ぢ"),
        ("du", "づ"),
        ("tsu", "つ"),
        ("fu", "ふ"),
        ("hu", "ふ"),
        ("ji", "じ"),
        ("zi", "じ"),
        ("si", "し"),
        ("ja", "じゃ"),
        ("jya", "じゃ"),
        ("sha", "しゃ"),
        ("sya", "しゃ"),
        ("cha", "ちゃ"),
        ("cya", "ちゃ"),
        ("tya", "ちゃ"),
        ("va", "ゔぁ"),
        ("vu", "ゔ"),
        ("wo", "を"),
        ("wa", "わ"),
        ("wi", "うぃ"),
        ("we", "うぇ"),
        ("ye", "いぇ"),
        ("whi", "うぃ"),
        ("qa", "くぁ"),
        ("tsa", "つぁ"),
        ("twu", "とぅ"),
        ("dwu", "どぅ"),
        ("fyu", "ふゅ"),
        ("xa", "ぁ"),
        ("li", "ぃ"),
        ("xu", "ぅ"),
        ("le", "ぇ"),
        ("xo", "ぉ"),
        ("xya", "ゃ"),
        ("lyu", "ゅ"),
        ("xyo", "ょ"),
        ("xwa", "ゎ"),
        ("lwa", "ゎ"),
        ("ca", "か"),
        ("ci", "し"),
        ("cu", "く"),
        ("ce", "せ"),
        ("co", "こ"),
        ("k", "k"),
        ("ky", "ky"),
        ("x", "x"),
    ],
)
def test_conversions(src, want):
    assert conv(src) == want


GOJUON = {
    "": "あいうえお", "k": "かきくけこ", "g": "がぎぐげご", "s": "さしすせそ", "z": "ざじずぜぞ",
    "t": "たちつてと", "d": "だぢづでど", "n": "なにぬねの", "h": "はひふへほ", "b": "ばびぶべぼ",
    "p": "ぱぴぷぺぽ", "m": "まみむめも", "r": "らりるれろ",
}
YOUON = {"k": "き", "g": "ぎ", "s": "し", "z": "じ", "t": "ち", "d": "ぢ", "n": "に", "h": "ひ",
         "b": "び", "p": "ぴ", "m": "み", "r": "り"}


def test_every_gojuon_and_youon():
    for c, row in GOJUON.items():
        for v, k in zip("aiueo", row):
            assert conv(c + v) == k, c + v
    for c, base in YOUON.items():
        for v, small in zip("aiueo", "ゃぃゅぇょ"):
            assert conv(c + "y" + v) == base + small, c + "y" + v


def test_sokuon_for_every_consonant():
    for c in "kgsztdhbpmrfjvwc":
        got = conv(c + c + "a")
        assert got.startswith("っ"), c
        assert got == "っ" + conv(c + "a")


def test_katakana():
    assert conv("kyouhaiitenkidesune", kata=True) == "キョウハイイテンキデスネ"
    assert conv("ra-men", kata=True) == "ラーメン"
    assert conv("vu", kata=True) == "ヴ"
    assert conv("xtu", kata=True) == "ッ"


def test_pending_and_backspace():
    r = kana.Romaji()
    assert r.feed("k") == "" and r.pending == "k"
    assert r.feed("y") == "" and r.pending == "ky"
    r.back()
    assert r.pending == "k"
    assert r.feed("a") == "か" and r.pending == ""
    assert r.feed("n") == "" and r.pending == "n"
    assert r.flush() == "ん" and r.pending == ""
    r.feed("s")
    r.feed("h")
    assert r.flush() == "sh"
    r.feed("t")
    r.clear()
    assert r.pending == "" and r.flush() == ""
