"""Record the raw material for docs/demo.gif.

Real Claude Code sessions run in a private tmux server with the mod loaded, talking to the real buddyd Hub and
HTTP API. Only the BLE transport is replaced: Link.serve() runs over an in-process pipe to a Cardputer made of
device/buddy_protocol.Protocol + device/buddy_ui_cp.BuddyUI drawing on a Pillow LCD. Hello and encryption run
end to end. Keys are pressed on that device by the scenario below.

    uv run --project daemon --with pillow python tools/demo/record.py OUT_DIR

Then `tools/demo/render.py OUT_DIR docs/demo.gif`.

Environment:
  DEMO_ROOT          work dir for homes and sockets (default /tmp/cbdemo; keep it short, it holds a unix socket)
  CLAUDE_BIN         claude executable (default: `claude` on PATH)
  DEMO_CLAUDE_JSON   your .claude.json; only the login/onboarding keys are copied into a throwaway config
  CLAUDE_SECURESTORAGE_CONFIG_DIR / CLAUDE_CONFIG_DIR
                     where your login lives, passed through so the throwaway config can use it
  DEMO_DEVICE_FONT   a monospace Japanese font standing in for EFontJA24 (default: M PLUS 1 Code)
"""

import asyncio
import getpass
import json
import os
import shlex
import shutil
import sys
import time
import types
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "daemon"))
sys.path.insert(0, str(REPO / "device"))

from cardbuddy import http_api  # noqa: E402
from cardbuddy.ble_link import Link  # noqa: E402
from cardbuddy.buddyd import Hub  # noqa: E402

ROOT = Path(os.environ.get("DEMO_ROOT", "/tmp/cbdemo"))
CLAUDE = os.environ.get("CLAUDE_BIN") or shutil.which("claude")
TMUX = shutil.which("tmux")
COLS, ROWS = 72, 26
PROJECTS = ("my-app", "api-server", "docs")
# Copied from the user's .claude.json so the throwaway config is logged in and skips onboarding.
CONFIG_KEYS = ("hasCompletedOnboarding", "lastOnboardingVersion", "lastReleaseNotesSeen", "oauthAccount", "userID",
               "hasAvailableSubscription", "effortCalloutDismissed", "effortCalloutV2Dismissed", "hasSeenTasksHint",
               "hasShownS1MWelcomeV2", "lastClawdEntranceVersion", "migrationVersion")
FALLBACK_FONTS = ("/System/Library/Fonts/Menlo.ttc", "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf")
FONT_CANDIDATES = ("~/Library/Fonts/Mplus1Code-Medium.otf", "~/Library/Fonts/MPLUS1Code-Medium.ttf",
                   "/usr/share/fonts/truetype/mplus/mplus-1m-medium.ttf")

FILES = {
    "my-app/app.py": '''def add(a, b):
    return a + b


def slugify(text):
    return "-".join(text.lower().split())
''',
    "my-app/test_app.py": '''import unittest

from app import add, slugify


class AppTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_slugify(self):
        self.assertEqual(slugify("Hello World"), "hello-world")

    def test_slugify_spaces(self):
        self.assertEqual(slugify("  a  b "), "a-b")


if __name__ == "__main__":
    unittest.main()
''',
}

t0 = time.monotonic()


def now():
    return round(time.monotonic() - t0, 3)


# ----- device

def rgb(c):
    return (c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF


class PilLcd:
    """The subset of M5.Lcd that buddy_ui_cp uses, at EFontJA24 x0.6 metrics (16px rows)."""

    FONTS = types.SimpleNamespace(EFontJA24="EFontJA24", DejaVu9="DejaVu9")

    def __init__(self, font, fallback):
        self.img = Image.new("RGB", (240, 135))
        self.d = ImageDraw.Draw(self.img)
        self.d.fontmode = "1"  # the real panel draws a bitmap font without antialiasing
        self.font, self.fallback = font, fallback
        self._missing = bytes(font.getmask("\U0010fffd"))
        self._fonts = {}
        self.fg, self.bg = 0xFFFFFF, None
        self.dirty = True

    def setFont(self, f):
        pass

    def setTextSize(self, s):
        pass

    def setTextColor(self, fg, bg=None):
        self.fg, self.bg = fg, bg

    def _font(self, ch):
        if ch not in self._fonts:
            missing = ch.isprintable() and not ch.isspace() and bytes(self.font.getmask(ch)) == self._missing
            self._fonts[ch] = self.fallback if missing else self.font
        return self._fonts[ch]

    def textWidth(self, s):
        return round(sum(self._font(ch).getlength(ch) for ch in s))

    def fillScreen(self, c):
        self.fillRect(0, 0, 240, 135, c)

    def fillRect(self, x, y, w, h, c):
        if w > 0 and h > 0:
            self.d.rectangle([x, y, x + w - 1, y + h - 1], fill=rgb(c))
            self.dirty = True

    def drawString(self, s, x, y):
        if self.bg is not None:
            self.fillRect(x, y, self.textWidth(s), 16, self.bg)
        for ch in s:
            f = self._font(ch)
            self.d.text((x, y - 1), ch, font=f, fill=rgb(self.fg))
            x += f.getlength(ch)
        self.dirty = True


def device_font():
    paths = [os.environ.get("DEMO_DEVICE_FONT")] + [os.path.expanduser(p) for p in FONT_CANDIDATES]
    path = next((p for p in paths if p and os.path.exists(p)), None)
    if path is None:
        raise SystemExit("record: no device font; set DEMO_DEVICE_FONT to a monospace Japanese font")
    fallback = next((p for p in FALLBACK_FONTS if os.path.exists(p)), path)
    return ImageFont.truetype(path, 14), ImageFont.truetype(fallback, 12)


class Device:
    def __init__(self, key, fonts):
        time.ticks_ms = lambda: int(time.monotonic() * 1000)
        time.ticks_diff = lambda a, b: a - b
        time.sleep_ms = lambda ms: None
        self.lcd = PilLcd(*fonts)
        sys.modules["M5"] = types.SimpleNamespace(Lcd=self.lcd)
        import buddy_protocol
        import buddy_ui_cp

        self.out: asyncio.Queue = asyncio.Queue()
        self.p = buddy_protocol.Protocol(key, self._send_line)
        self.ui = buddy_ui_cp.BuddyUI(self.p)
        self.ui.set_connection("connected")
        self.frames = []  # (t, Image)
        self.keys = []  # (t, key)

    def _send_line(self, line):
        self.out.put_nowait(line.rstrip(b"\n"))
        return True

    def tick(self):
        self.ui.refresh()
        if self.lcd.dirty:
            self.lcd.dirty = False
            self.frames.append((now(), self.lcd.img.copy()))

    def key(self, k):
        self.keys.append((now(), k))
        self.ui.on_key({"ENTER": 0x0A, "ESC": 0x1B, "TAB": 0x2B}.get(k, k))
        self.tick()

    async def run(self):
        while True:
            self.tick()
            await asyncio.sleep(0.04)


class Pipe:
    """The conn that Link.serve() expects, wired to the in-process device instead of BLE."""

    def __init__(self, dev):
        self.dev = dev

    async def send_line(self, line):
        self.dev.p.on_line(line.rstrip(b"\n"))
        self.dev.tick()

    async def recv_line(self):
        return await self.dev.out.get()


# ----- terminal

def tmux(*args):
    return [TMUX, "-L", "cbdemo", *args]


async def run(*cmd):
    p = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE)
    out, _ = await p.communicate()
    return out.decode()


def setup():
    if not CLAUDE or not TMUX:
        raise SystemExit("record: need claude and tmux on PATH (or CLAUDE_BIN)")
    src = os.environ.get("DEMO_CLAUDE_JSON")
    if not src:
        raise SystemExit("record: set DEMO_CLAUDE_JSON to your .claude.json (for the login only)")
    if ROOT.exists():
        shutil.rmtree(ROOT)
    for rel, body in FILES.items():
        (ROOT / "home" / rel).parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "home" / rel).write_text(body)
    for p in PROJECTS:
        (ROOT / "home" / p).mkdir(parents=True, exist_ok=True)
    (ROOT / "cb").mkdir(mode=0o700)
    (ROOT / "cfg").mkdir()
    full = json.loads(Path(src).read_text())
    cfg = {k: full[k] for k in CONFIG_KEYS if k in full}
    cfg.update(theme="dark", autoUpdates=False,
               projects={str(ROOT / "home" / p): {"hasTrustDialogAccepted": True} for p in PROJECTS})
    (ROOT / "cfg" / ".claude.json").write_text(json.dumps(cfg))
    (ROOT / "tmux.conf").write_text("set -g status off\nset -g focus-events on\nset -g mouse on\nset -g default-terminal xterm-256color\n")


def claude_env():
    secure = os.environ.get("CLAUDE_SECURESTORAGE_CONFIG_DIR") or os.environ.get("CLAUDE_CONFIG_DIR")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.dirname(TMUX),
        "HOME": os.environ["HOME"], "USER": getpass.getuser(), "LOGNAME": getpass.getuser(),
        "TERM": "xterm-256color", "COLORTERM": "truecolor", "LANG": "en_US.UTF-8", "SHELL": "/bin/bash",
        "CLAUDE_CONFIG_DIR": str(ROOT / "cfg"), "CLAUDE_CODE_HIDE_CWD": "1", "CARDBUDDY_HOME": str(ROOT / "cb"),
    }
    if secure:
        env["CLAUDE_SECURESTORAGE_CONFIG_DIR"] = secure
    return env


def claude_cmd():
    return shlex.join([CLAUDE, "--plugin-dir", str(REPO / "mod"), "--permission-mode", "default", "--model", "sonnet",
                       "--append-system-prompt", "Keep every reply to one or two short sentences."])


async def start_window(i, project):
    cwd = str(ROOT / "home" / project)
    if i == 0:
        p = await asyncio.create_subprocess_exec(
            *tmux("-f", str(ROOT / "tmux.conf"), "new-session", "-d", "-s", "demo", "-n", "w0",
                  "-x", str(COLS), "-y", str(ROWS), "-c", cwd, claude_cmd()), env=claude_env())
        await p.wait()
    else:
        await run(*tmux("new-window", "-d", "-t", "demo", "-n", f"w{i}", "-c", cwd, claude_cmd()))


async def type_text(win, text, delay=0.03):
    for ch in text:
        await run(*tmux("send-keys", "-t", f"demo:{win}", "-l", ch))
        await asyncio.sleep(delay)
    await asyncio.sleep(0.4)
    await run(*tmux("send-keys", "-t", f"demo:{win}", "Enter"))


async def record_terminal(frames):
    last = None
    while True:
        s = await run(*tmux("capture-pane", "-p", "-e", "-t", "demo:w0"))
        if s != last:
            last = s
            frames.append((now(), s))
        await asyncio.sleep(0.1)


# ----- scenario

class Demo:
    def __init__(self, hub, dev):
        self.hub, self.dev = hub, dev
        self.marks = []

    def mark(self, name):
        self.marks.append((now(), name))
        print(f"{now():7.2f} {name}", flush=True)

    async def until(self, cond, timeout=180):
        end = time.monotonic() + timeout
        while not cond():
            if time.monotonic() > end:
                raise TimeoutError(getattr(cond, "__name__", "condition"))
            await asyncio.sleep(0.05)

    def state(self, n):
        s = next((s for s in self.hub.table.sessions.values() if s["n"] == n), None)
        return s and s["state"]

    async def press(self, *keys, gap=0.25):
        for k in keys:
            self.dev.key(k)
            await asyncio.sleep(gap)

    async def answer_loop(self):
        """Answer whatever the device shows: Y for a perm, the second option for an ask."""
        while True:
            q = self.dev.p.queue
            if q and self.dev.ui.mode != "input":
                head = q[0]
                self.mark(f"{head['t']}_shown")
                await asyncio.sleep(2.0)
                if head["t"] == "perm":
                    await self.press("y")
                else:
                    for _ in head["qs"]:
                        await self.press(".", gap=0.6)
                        await self.press("ENTER", gap=0.6)
                self.mark(f"{head['t']}_answered")
                # Protocol replaces its queue list on every change, so re-read it rather than holding q
                await self.until(lambda: not self.dev.p.queue or self.dev.p.queue[0] is not head, 10)
            await asyncio.sleep(0.05)

    async def turn(self, n, start, mark="type"):
        """Run one turn of session n and wait for it to finish."""
        self.mark(mark)
        await start()
        await self.until(lambda: self.state(n) == "running", 60)
        self.mark("wait")
        await self.until(lambda: self.state(n) == "idle" and not self.dev.p.queue)
        self.mark("done")

    async def run(self):
        for i, project in enumerate(PROJECTS):
            await start_window(i, project)
            await self.until(lambda: len(self.hub.table.sessions) == i + 1, 60)
        await asyncio.sleep(3)
        side = ["In one sentence: what makes an HTTP method idempotent?",
                "Suggest a one-line tagline for a pocket companion device for Claude Code."]
        await asyncio.gather(*(self.turn(n, lambda w=w, t=t: type_text(w, t, 0.005))
                               for n, w, t in ((2, "w1", side[0]), (3, "w2", side[1]))))
        await asyncio.sleep(2)
        answering = asyncio.create_task(self.answer_loop())

        self.mark("start")
        await asyncio.sleep(1.5)
        await self.press(".", ".", gap=0.7)
        await self.press("1", gap=1.2)

        self.mark("perm")
        await self.turn(1, lambda: type_text("w0", "Run `python3 -m unittest -v` and report the result."))
        await asyncio.sleep(2)

        self.mark("ask")
        await self.turn(1, lambda: type_text(
            "w0", "Ask me which feature to add next using AskUserQuestion, then just confirm my choice."))
        await asyncio.sleep(2)

        self.mark("kana")

        async def kana():
            await self.press("1", "ENTER", gap=0.5)
            await self.press("TAB", gap=0.6)
            await self.press(*"tesutowojikkoushite", gap=0.09)
            await asyncio.sleep(0.9)
            await self.press("ENTER", gap=0.5)

        await self.turn(1, kana, "device_type")
        await asyncio.sleep(1.5)

        self.mark("log")
        await self.press("l", gap=0.5)
        await asyncio.sleep(4)
        self.mark("end")
        answering.cancel()


async def main(out: Path):
    setup()
    key = os.urandom(32)
    hub = Hub()
    hub.link = link = Link(key, hub)
    dev = Device(key, device_font())
    term = []
    server = await http_api.serve(hub, str(ROOT / "cb" / "buddyd.sock"))
    bg = [asyncio.create_task(c) for c in (link.serve(Pipe(dev)), hub.sessions_loop(), hub.expire_loop(),
                                            dev.run(), record_terminal(term))]
    demo = Demo(hub, dev)
    try:
        await demo.run()
    finally:
        for t in bg:
            t.cancel()
        server.close()
        await run(*tmux("kill-server"))
        await asyncio.sleep(3)  # claude writes its transcripts while exiting
        shutil.rmtree(ROOT / "cfg", ignore_errors=True)  # holds a copy of the account profile
        out.mkdir(parents=True, exist_ok=True)
        (out / "dev").mkdir(exist_ok=True)
        with open(out / "term.jsonl", "w") as f:
            for t, s in term:
                f.write(json.dumps({"t": t, "ansi": s}, ensure_ascii=False) + "\n")
        with open(out / "dev.jsonl", "w") as f:
            for i, (t, img) in enumerate(dev.frames):
                img.save(out / "dev" / f"{i:05d}.png")
                f.write(json.dumps({"t": t, "f": f"{i:05d}.png"}) + "\n")
        (out / "marks.json").write_text(json.dumps(demo.marks))
        (out / "keys.json").write_text(json.dumps(dev.keys))


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1])))
