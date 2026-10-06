"""Compose docs/demo.gif from a tools/demo/record.py recording.

    uv run --no-project --with pillow --with pyte python tools/demo/render.py REC_DIR docs/demo.gif [KEYFRAME_DIR]

Fails if any frame still shows account details after the banner lines are dropped (see DEMO_FORBIDDEN).
"""

import getpass
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pyte
from PIL import Image, ImageDraw, ImageFont

FPS = 10
W, H = 1200, 600
COLS, ROWS = 72, 26
WAIT_PLAY = 0.8
ANSWER_GRACE = 0.8
DONE_GRACE = 0.8
CAPTIONS = {
    "start": "1/4  Every Claude Code session shows up on the Cardputer",
    "perm": "2/4  Permission prompt: read the full command and approve on the device",
    "ask": "3/4  AskUserQuestion: pick an answer on the device",
    "kana": "4/4  Type a prompt on the device (romaji to kana) and send it",
    "log": "4/4  ...and read Claude's reply on the device",
}
# Lines that only Claude Code's start-up banner prints and that carry the account or organization.
BANNER_DROP = re.compile(r"^\s*▎|login expires")
# Add your email, organization and the like as DEMO_FORBIDDEN=a,b,c; the user name and home are always checked.
FORBIDDEN = re.compile("|".join(re.escape(w) for w in [getpass.getuser(), str(Path.home()), "/Users/", "/home/", "@",
                                                       *filter(None, os.environ.get("DEMO_FORBIDDEN", "").split(","))]),
                       re.I)
SUBST = {"⏺": "●", "⏸": "‖", "⏵": "▶"}
SGR = re.compile(r"\x1b\[[0-9;:]*m")

BG = (24, 24, 27)
TERM_BG = (30, 30, 30)
TERM_FG = (212, 212, 212)
ORANGE = (204, 120, 92)
NAMED = {
    "black": (0, 0, 0), "red": (205, 49, 49), "green": (13, 188, 121), "brown": (229, 229, 16),
    "blue": (36, 114, 200), "magenta": (188, 63, 188), "cyan": (17, 168, 205), "white": (229, 229, 229),
    "brightblack": (102, 102, 102), "brightred": (241, 76, 76), "brightgreen": (35, 209, 139),
    "brightbrown": (245, 245, 67), "brightblue": (59, 142, 234), "brightmagenta": (214, 112, 214),
    "brightcyan": (41, 184, 219), "brightwhite": (229, 229, 229),
}


def font(paths, size, index=0):
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.exists(p):
            return ImageFont.truetype(p, size, index=index)
    raise SystemExit(f"render: none of {paths} found")


MENLO = "/System/Library/Fonts/Menlo.ttc"
MONO = font([MENLO], 14)
MONO_B = font([MENLO], 14, index=1)
CJK = font(["~/Library/Fonts/Mplus1Code-Medium.otf", "/System/Library/Fonts/Hiragino Sans GB.ttc"], 16)
FALLBACK = [font([p], 14) for p in ("/System/Library/Fonts/Apple Symbols.ttf",
                                    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf")]
UI = font(["/System/Library/Fonts/Helvetica.ttc"], 24, index=1)
UI_S = font(["/System/Library/Fonts/Helvetica.ttc"], 15)
KEY = font([MENLO], 11)
CW = MONO.getlength("M")
LH = 18


def _notdef(f):
    return bytes(f.getmask("\U0010fffd"))


_chain = [(f, _notdef(f)) for f in (MONO, CJK, *FALLBACK)]
_pick = {}


def glyph_font(ch, bold):
    if ch not in _pick:
        _pick[ch] = next((f for f, nd in _chain if bytes(f.getmask(ch)) != nd), MONO)
    f = _pick[ch]
    return MONO_B if bold and f is MONO else f


def color(c, default):
    if c == "default":
        return default
    if c in NAMED:
        return NAMED[c]
    if re.fullmatch(r"[0-9a-fA-F]{6}", c):
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    return default


def redact(ansi):
    out = []
    for line in ansi.split("\n")[:ROWS]:
        plain = SGR.sub("", line)
        if BANNER_DROP.search(plain):
            continue
        if not plain.strip() and out and not SGR.sub("", out[-1]).strip():
            continue
        out.append(line)
    return out


def term_image(ansi):
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stream.feed("\r\n".join(redact(ansi)))
    plain = "\n".join(screen.display)
    if FORBIDDEN.search(plain):
        raise SystemExit(f"render: sensitive text on screen: {FORBIDDEN.search(plain).group()!r}\n{plain}")
    pad, bar = 14, 28
    img = Image.new("RGB", (round(COLS * CW) + 2 * pad, ROWS * LH + bar + 2 * 10), TERM_BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, img.width, bar], fill=(50, 50, 52))
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse([12 + i * 20, 9, 24 + i * 20, 21], fill=c)
    d.text((img.width / 2, bar / 2), "claude — my-app", font=UI_S, fill=(190, 190, 190), anchor="mm")
    for glyphs in (False, True):  # a wide glyph spills into the next cell, so paint every background first
        for y in range(ROWS):
            row = screen.buffer[y]
            for x in range(COLS):
                ch = row[x]
                fg, bg = color(ch.fg, TERM_FG), color(ch.bg, TERM_BG)
                if ch.reverse:
                    fg, bg = bg, fg
                px, py = pad + x * CW, bar + 10 + y * LH
                s = SUBST.get(ch.data, ch.data)
                if not glyphs and bg != TERM_BG:
                    d.rectangle([px, py, px + CW, py + LH - 1], fill=bg)
                elif glyphs and s and s != " ":
                    d.text((px, py + 2), s, font=glyph_font(s, ch.bold), fill=fg)
    return img


ROWS_KEYS = [list("`1234567890-=") + ["del"], ["tab"] + list("qwertyuiop[]\\"),
             ["fn", "shift"] + list("asdfghjkl;'") + ["ent"], ["ctrl", "opt", "alt"] + list("zxcvbnm,./") + ["spc"]]
KEY_OF = {"ENTER": "ent", "TAB": "tab", "ESC": "`", " ": "spc"}


def device_image(screen, pressed):
    s = 2
    sw, sh = 240 * s, 135 * s
    bw = sw + 60
    kh = 4 * 30 + 20
    img = Image.new("RGBA", (bw, 40 + sh + 24 + kh + 20), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, bw - 1, img.height - 1], 26, fill=(58, 58, 62), outline=(90, 90, 95), width=2)
    d.rounded_rectangle([18, 22, bw - 19, 22 + sh + 36], 10, fill=(12, 12, 12))
    img.paste(screen.resize((sw, sh), Image.NEAREST), (30, 40))
    ky = 40 + sh + 34
    for r, keys in enumerate(ROWS_KEYS):
        kw = (bw - 40) / len(keys)
        for i, k in enumerate(keys):
            x0, y0 = 20 + i * kw, ky + r * 30
            on = k == pressed
            d.rounded_rectangle([x0 + 2, y0, x0 + kw - 3, y0 + 25], 5,
                                fill=ORANGE if on else (34, 34, 37), outline=(80, 80, 85))
            d.text((x0 + kw / 2, y0 + 13), k, font=KEY, fill=(255, 255, 255) if on else (170, 170, 170), anchor="mm")
    return img


def timeline(marks):
    """Source timestamps to sample for each output frame, with whether that frame is fast-forwarded.

    Typing plays at 4x in the terminal and 1.5x on the device, and waits for Claude are squeezed into a fixed short time;
    anything shown on the device plays in real time.
    """
    start = next(ts for ts, name in marks if name == "start")
    end = next(ts for ts, name in marks if name == "end")
    spans = []
    for (ts, name), (nxt, _) in zip(marks, marks[1:]):
        if name == "type":
            spans.append((ts, nxt, 4))
        elif name == "device_type":
            spans.append((ts, nxt, 1.5))
        elif name == "wait":
            spans.append((ts, nxt, (nxt - ts) / WAIT_PLAY))
        elif name.endswith("_answered"):
            spans.append((ts + ANSWER_GRACE, nxt, (nxt - ts - ANSWER_GRACE) / WAIT_PLAY))
        elif name == "done":
            spans.append((ts + DONE_GRACE, nxt, (nxt - ts - DONE_GRACE) / 0.4))
    out, t = [], start
    while t < end:
        speed = next((sp for a, b, sp in spans if a <= t < b), 1)
        speed = max(speed, 1)
        out.append((t, speed > 1.5))
        t += speed / FPS
    return out


def latest(items, t):
    cur = items[0]
    for it in items:
        if it[0] > t:
            break
        cur = it
    return cur


def main():
    rec, gif = Path(sys.argv[1]), Path(sys.argv[2])
    keyframes = Path(sys.argv[3]) if len(sys.argv) > 3 else None
    marks = json.loads((rec / "marks.json").read_text())
    keys = json.loads((rec / "keys.json").read_text()) if (rec / "keys.json").exists() else []
    term = [(j["t"], j["ansi"]) for j in map(json.loads, (rec / "term.jsonl").read_text().splitlines())]
    dev = [(j["t"], j["f"]) for j in map(json.loads, (rec / "dev.jsonl").read_text().splitlines())]
    caption_marks = [(ts, name) for ts, name in marks if name in CAPTIONS]
    term_cache, dev_cache = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        frames = timeline(marks)
        frames += [(frames[-1][0], False)] * (FPS * 3 // 2)
        kf_times = sorted([(ts + 0.5, n) for ts, n in caption_marks]
                          + [(ts + 1.5, n) for ts, n in marks if n.endswith("_shown")]
                          + [(ts + 1.0, n) for ts, n in marks if n.endswith("_answered")]
                          + [(ts + 3.0, n) for ts, n in marks if n == "device_type"]
                          + [(frames[-1][0], "end")])
        for i, (t, fast) in enumerate(frames):
            ta = latest(term, t)
            if ta[0] not in term_cache:
                term_cache[ta[0]] = term_image(ta[1])
            df = latest(dev, t)[1]
            if df not in dev_cache:
                dev_cache[df] = Image.open(rec / "dev" / df).convert("RGB")
            recent = [k for kt, k in keys if t - 0.35 <= kt <= t]
            pressed = KEY_OF.get(recent[-1], recent[-1].lower()) if recent else None
            canvas = Image.new("RGB", (W, H), BG)
            d = ImageDraw.Draw(canvas)
            caption = latest(caption_marks, t)[1]
            d.text((W / 2, 34), CAPTIONS[caption], font=UI, fill=(240, 238, 230), anchor="mm")
            if fast:
                d.text((W - 24, 34), "▶▶ fast-forward", font=UI_S, fill=ORANGE, anchor="rm")
            ti = term_cache[ta[0]]
            canvas.paste(ti, (20, 68))
            di = device_image(dev_cache[df], pressed)
            dx = 20 + ti.width + (W - 20 - ti.width - di.width) // 2
            canvas.paste(di, (dx, 68), di)
            d.text((dx + di.width / 2, 68 + di.height + 12), "Cardputer UI: the repo's device/ code, rendered on a PC",
                   font=UI_S, fill=(140, 140, 140), anchor="mm")
            canvas.save(f"{tmp}/{i:05d}.png")
            while keyframes and kf_times and kf_times[0][0] <= t:
                keyframes.mkdir(parents=True, exist_ok=True)
                canvas.save(keyframes / f"{i:05d}_{kf_times.pop(0)[1]}.png")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(FPS), "-i", f"{tmp}/%05d.png",
                        "-vf", "split[a][b];[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=none:diff_mode=rectangle",
                        "-loop", "0", str(gif)], check=True)
    print(f"{gif}: {len(frames)} frames, {gif.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
