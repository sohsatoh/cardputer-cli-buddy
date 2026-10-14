import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")

HARNESS = Path(__file__).parent / "js" / "markdown_harness.js"
LINK = {"target": "_blank", "rel": "noopener noreferrer"}


def run(md=None, log=None, time=None):
    res = subprocess.run(["node", str(HARNESS)], input=json.dumps({"md": md or {}, "log": log or [], "time": time or {}}),
                         capture_output=True, text=True, check=True, timeout=30)
    return json.loads(res.stdout)


def render(text):
    return run(md={"x": text})["md"]["x"]


def tags(tree):
    if isinstance(tree, str):
        return set()
    out = set()
    for node in tree:
        if isinstance(node, list):
            out.add(node[0])
            out |= tags(node[1:])
    return out


def test_headings_and_paragraphs():
    assert render("# H1\n### H3\n本文\n2 行目\n\n次の段落") == [
        ["h1", "H1"], ["h3", "H3"], ["p", "本文\n2 行目"], ["p", "次の段落"]]


def test_code_block_is_not_interpreted():
    assert render("```python\n**x**\n# y\n<b>\n```\n後") == [["pre", ["code", "**x**\n# y\n<b>"]], ["p", "後"]]
    assert render("```\n閉じていない\n- a") == [["pre", ["code", "閉じていない\n- a"]]]


def test_inline_emphasis_and_code():
    assert render("a **b** *c* _d_ `e*f*` snake_case_name 2 * 3 * 4") == [
        ["p", "a ", ["strong", "b"], " ", ["em", "c"], " ", ["em", "d"], " ", ["code", "e*f*"],
         " snake_case_name 2 * 3 * 4"]]
    assert render(r"\*not\* **太字の中の *斜体***") == [
        ["p", "*not* ", ["strong", "太字の中の ", ["em", "斜体"]]]]


def test_lists_nest_by_indent():
    assert render("- a\n  - b\n  - c\n- d\n\n1. x\n2. y\n   * z") == [
        ["ul", ["li", "a", ["ul", ["li", "b"], ["li", "c"]]], ["li", "d"]],
        ["ol", ["li", "x"], ["li", "y", ["ul", ["li", "z"]]]]]
    assert render("3. three\n4. four") == [["ol", {"start": "3"}, ["li", "three"], ["li", "four"]]]


def test_blockquote_and_rule():
    assert render("> q\n> **r**\n\n---\n後") == [["blockquote", ["p", "q\n", ["strong", "r"]]], ["hr"], ["p", "後"]]


def test_table_is_wrapped_for_horizontal_scroll():
    assert render("| a | b |\n|---|:-:|\n| 1 | `x\\|y` |\n| 2 |") == [
        ["div", {"class": "table-wrap"}, ["table",
                                          ["thead", ["tr", ["th", "a"], ["th", "b"]]],
                                          ["tbody", ["tr", ["td", "1"], ["td", ["code", "x|y"]]],
                                           ["tr", ["td", "2"], ["td"]]]]]]


def test_html_stays_text():
    tree = render("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n**<b>x</b>**")
    assert tree == [["p", "<script>alert(1)</script>"], ["p", "<img src=x onerror=alert(1)>"],
                    ["p", ["strong", "<b>x</b>"]]]
    assert tags(tree) == {"p", "strong"}


def test_only_http_links_become_links():
    tree = render("[x](javascript:alert(1)) [y](https://example.com/a?b=1) [z](/rel) [w](data:text/html,hi) "
                  "[v](JaVaScRiPt:alert(1)) [**u**](http://e.com)")
    assert tree == [["p", "[x](javascript:alert(1)) ", ["a", {"href": "https://example.com/a?b=1", **LINK}, "y"],
                     " (example.com) [z](/rel) [w](data:text/html,hi) [v](JaVaScRiPt:alert(1)) ",
                     ["a", {"href": "http://e.com/", **LINK}, ["strong", "u"]], " (e.com)"]]


def test_link_label_cannot_hide_where_it_goes():
    assert render("[https://e.com/x](https://e.com/x) [e.com](https://e.com)") == [
        ["p", ["a", {"href": "https://e.com/x", **LINK}, "https://e.com/x"], " ",
         ["a", {"href": "https://e.com/", **LINK}, "e.com"]]]
    assert render("[bank.example\u202ecom](https://evil.example:8443/)") == [
        ["p", ["a", {"href": "https://evil.example:8443/", **LINK},
               "bank.example", ["span", {"class": "ctl"}, "\\u{202E}"], "com"], " (evil.example:8443)"]]


def test_images_are_not_loaded():
    tree = render("![図](https://e.com/i.png) ![bad](javascript:x)")
    assert tree == [["p", ["a", {"href": "https://e.com/i.png", **LINK}, "図"], " (e.com) bad"]]
    assert "img" not in tags(tree)


def test_backticks_in_info_string_do_not_open_a_fence():
    assert render("```foo```\nnext") == [["p", ["code", "foo"], "\nnext"]]
    assert render("~~~ `x`\ncode\n~~~") == [["pre", ["code", "code"]]]


def test_only_assistant_text_is_markdown():
    out = run(log=[["user", "**a**"], ["assistant", "**a**"]])["log"]
    assert out[0] == ["div", {"class": "log-item u"}, ["div", {"class": "who"}, "あなた"],
                      ["div", {"class": "x"}, "**a**"]]
    assert out[1] == ["div", {"class": "log-item a"}, ["div", {"class": "who"}, "Claude"],
                      ["div", {"class": "x md"}, ["p", ["strong", "a"]]]]


def nested(depth, wrap, inner):
    for _ in range(depth):
        inner = wrap(inner)
    return inner


def test_deep_nesting_stops_at_a_fixed_depth():
    assert render(">" * 4000) == [nested(17, lambda x: ["blockquote", x], ["p", ">" * (4000 - 17)])]
    assert render("- " * 2000 + "x") == [nested(17, lambda x: ["ul", ["li", x]], "- " * (2000 - 17) + "x")]
    assert render("- " * 2000) == [["hr"]]
    assert render("*" * 3000 + "a" + "*" * 3000)[0][0] == "p"


def test_line_separators_do_not_make_block_regexes_backtrack():
    ms = run(time={name: lead + " " * 4000 + "\u2028" for name, lead in
                   {"heading": "#", "item": "-", "quote": ">", "fence": "```"}.items()})["ms"]
    assert all(v < 100 for v in ms.values()), ms


def test_long_text_is_shown_as_is():
    text = "**a** " + "b" * 8000
    assert render(text) == [text]
    assert render(text[:8000])[0][:2] == ["p", ["strong", "a"]]
