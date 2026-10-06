import asyncio
import json
import logging
import os
import plistlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from cardbuddy import buddyd, cli, http_api
from cardbuddy.buddyd import Hub


class FakeSerial:
    """MicroPython の raw REPL を模擬する。"""

    flash: dict = {}
    fail = False

    def __init__(self, port, baud, timeout):
        self.timeout = timeout
        self.out = b""
        self.raw = False
        self.code = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def reset_input_buffer(self):
        self.out = b""

    def write(self, data):
        for b in data:
            c = bytes([b])
            if not self.raw:
                if c == b"\x01":
                    self.raw = True
                    self.out += b"raw REPL; CTRL-B to exit\r\n>"
            elif c == b"\x04":
                self._run(self.code.decode())
                self.code = b""
            elif c == b"\x02":
                self.raw = False
            else:
                self.code += c

    def _run(self, code):
        if "config('mac')" in code:
            self.out += b"OKClaude_A1B2C3\r\n\x04\x04>"
            return
        if self.fail:
            self.out += b"OK\x04Traceback (most recent call last):\r\nOSError: 28\r\n\x04>"
            return
        hexdata = re.search(r"unhexlify\('([0-9a-f]+)'\)", code).group(1)
        path = re.search(r"open\('([^']+)'", code).group(1)
        assert "del " in code
        name = {"/flash/cardbuddy.key": "key", "/flash/cardbuddy_wifi.json": "wifi"}[path]
        FakeSerial.flash[name] = bytes.fromhex(hexdata)
        self.out += b"OK%d\r\n\x04\x04>" % len(FakeSerial.flash[name])

    def read_until(self, token):
        i = self.out.find(token)
        if i < 0:
            data, self.out = self.out, b""
            return data
        data, self.out = self.out[:i + len(token)], self.out[i + len(token):]
        return data


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="cb")
    monkeypatch.setenv("CARDBUDDY_HOME", os.path.join(d, "h"))
    monkeypatch.setattr(cli.serial, "Serial", FakeSerial)
    FakeSerial.flash, FakeSerial.fail = {}, False
    return Path(d) / "h"


def test_pair_generates_key_and_writes_device(home, capsys, caplog):
    caplog.set_level(logging.DEBUG)
    assert cli.main(["pair", "--port", "/dev/fake"]) == 0
    key = (home / "key").read_bytes()
    assert len(key) == 32 and FakeSerial.flash["key"] == key
    assert stat.S_IMODE((home / "key").stat().st_mode) == 0o600
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    out = capsys.readouterr()
    assert key.hex() not in out.out + out.err + caplog.text

    FakeSerial.flash = {}
    assert cli.main(["pair", "--port", "/dev/fake"]) == 0
    assert (home / "key").read_bytes() == key == FakeSerial.flash["key"]

    assert cli.main(["pair", "--port", "/dev/fake", "--rotate"]) == 0
    new = (home / "key").read_bytes()
    assert new != key and FakeSerial.flash["key"] == new
    assert stat.S_IMODE((home / "key").stat().st_mode) == 0o600


def test_pair_wifi_writes_credentials_without_echoing_password(home, capsys, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr("builtins.input", lambda prompt="": "home-net")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt="": "s3cret-pw")
    assert cli.main(["pair", "--port", "/dev/fake", "--wifi"]) == 0
    assert json.loads(FakeSerial.flash["wifi"]) == {"ssid": "home-net", "psk": "s3cret-pw"}
    assert len(FakeSerial.flash["key"]) == 32
    out = capsys.readouterr()
    assert "s3cret-pw" not in out.out + out.err + caplog.text
    assert "s3cret-pw".encode().hex() not in out.out + out.err + caplog.text


def test_pair_keeps_host_key_unchanged_when_device_write_fails(home, capsys):
    FakeSerial.fail = True
    with pytest.raises(SystemExit) as e:
        cli.main(["pair", "--port", "/dev/fake"])
    assert "OSError" in str(e.value)
    assert not (home / "key").exists()


def test_load_key_errors(home):
    path = home / "key"
    with pytest.raises(SystemExit, match="buddy pair"):
        buddyd.load_key(path)
    home.mkdir(mode=0o700)
    path.write_bytes(os.urandom(32))
    path.chmod(0o644)
    with pytest.raises(SystemExit, match="0600"):
        buddyd.load_key(path)
    path.chmod(0o600)
    assert len(buddyd.load_key(path)) == 32
    path.write_bytes(b"short")
    with pytest.raises(SystemExit, match="32"):
        buddyd.load_key(path)


def test_buddyd_exits_without_key(home):
    exe = Path(sys.executable).parent / "buddyd"
    p = subprocess.run([str(exe)], capture_output=True, text=True, timeout=30,
                       env={**os.environ, "CARDBUDDY_HOME": str(home)})
    assert p.returncode != 0 and "buddy pair" in p.stderr


class FakeLink:
    connected = True
    device = "Claude_ab12cd"

    def send(self, msg):
        pass


def test_status(home, capsys):
    loop = asyncio.new_event_loop()
    hub = Hub(FakeLink())
    hub.table.upsert("0f3c9a1e-sid", "/w", "", "idle", 0)
    server = loop.run_until_complete(http_api.serve(hub, str(home / "buddyd.sock")))
    t = threading.Thread(target=loop.run_forever)
    t.start()
    try:
        assert cli.main(["status"]) == 0
    finally:
        loop.call_soon_threadsafe(loop.stop)
        t.join()
        server.close()
        loop.close()
    out = capsys.readouterr().out
    assert "Claude_ab12cd" in out and "0f3c9a1e-sid" in out and "idle" in out


def test_status_when_not_running(home, capsys):
    assert cli.main(["status"]) == 1
    assert "not running" in capsys.readouterr().err


def test_install_agent(home, monkeypatch, capsys):
    fake_home = home.parent / "user"
    monkeypatch.setenv("HOME", str(fake_home))
    assert cli.main(["install-agent"]) == 0
    plist = fake_home / "Library/LaunchAgents/com.sohsatoh.cardbuddy.buddyd.plist"
    p = plistlib.loads(plist.read_bytes())
    assert p["Label"] == "com.sohsatoh.cardbuddy.buddyd"
    assert p["ProgramArguments"] == [str(Path(sys.executable).parent / "buddyd")]
    assert p["KeepAlive"] is True
    assert p["StandardErrorPath"] == str(home / "buddyd.log")
    assert p["EnvironmentVariables"] == {"CARDBUDDY_HOME": str(home)}
    out = capsys.readouterr().out
    assert f"launchctl bootstrap gui/{os.getuid()} {plist}" in out


def test_single_instance_lock(home):
    home.mkdir(mode=0o700)
    lock = buddyd.lock_or_exit(home)
    with pytest.raises(SystemExit, match="already running"):
        buddyd.lock_or_exit(home)
    lock.close()
    buddyd.lock_or_exit(home).close()



def test_device_name_snippet_matches_device_rule(monkeypatch, capsys):
    import types
    ble = types.SimpleNamespace(active=lambda *a: True, config=lambda k: (0, b"\x02\x00\x00\xa1\xb2\xc3"))
    monkeypatch.setitem(sys.modules, "bluetooth", types.SimpleNamespace(BLE=lambda: ble))
    exec(cli.NAME_CODE, {})
    assert capsys.readouterr().out == "Claude_A1B2C3\n"


def test_pair_saves_device_name(home):
    assert cli.main(["pair", "--port", "/dev/fake"]) == 0
    assert (home / "device").read_text() == "Claude_A1B2C3"
    assert stat.S_IMODE((home / "device").stat().st_mode) == 0o600


def test_resolve_name(home):
    assert buddyd.resolve_name("Claude_AAAAAA") == "Claude_AAAAAA"
    assert buddyd.resolve_name(None) is None
    home.mkdir(mode=0o700)
    (home / "device").write_text("Claude_A1B2C3\n")
    assert buddyd.resolve_name(None) == "Claude_A1B2C3"
    assert buddyd.resolve_name("Claude_AAAAAA") == "Claude_AAAAAA"


def test_amain_removes_stale_wavs(home, monkeypatch):
    async def done(*a):
        return None

    monkeypatch.setattr(buddyd.Link, "run", done)
    monkeypatch.setattr(buddyd.Hub, "sessions_loop", done)
    monkeypatch.setattr(buddyd.Hub, "expire_loop", done)
    (home / "tmp").mkdir(parents=True)
    (home / "tmp" / "old.wav").write_bytes(b"x")
    (home / "tmp" / "keep.txt").write_bytes(b"x")
    asyncio.run(buddyd._amain(os.urandom(32), None))
    assert sorted(p.name for p in (home / "tmp").iterdir()) == ["keep.txt"]


SIGTERM_SCRIPT = """
import asyncio, os, sys, types
from cardbuddy import buddyd

async def run(self, name):
    hub = self.hub
    async def transcribe(wav, lang):
        with open(os.environ["MARK"], "w") as f:
            f.write(wav)
        await asyncio.Event().wait()
    hub.stt = types.SimpleNamespace(transcribe=transcribe, SttError=Exception)
    hub.on_msg({"t": "voice_begin", "vid": "v1", "lang": "ja-JP"})
    hub.on_audio(b"\\x00\\x00" + b"\\x00" * 1600)
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await asyncio.Event().wait()

buddyd.Link.run = run
sys.argv = ["buddyd"]
buddyd.main()
"""


def test_sigterm_runs_cleanup(home):
    import signal
    import time
    home.mkdir(mode=0o700)
    (home / "key").write_bytes(os.urandom(32))
    (home / "key").chmod(0o600)
    mark = home / "mark"
    p = subprocess.Popen([sys.executable, "-c", SIGTERM_SCRIPT],
                         env={**os.environ, "CARDBUDDY_HOME": str(home), "MARK": str(mark)},
                         stderr=subprocess.PIPE, text=True)
    try:
        for _ in range(200):
            if mark.exists() and mark.read_text():
                break
            time.sleep(0.05)
        wav = mark.read_text()
        assert os.path.exists(wav)
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=10) == 0, p.stderr.read()
    finally:
        p.kill()
    assert not os.path.exists(wav)
