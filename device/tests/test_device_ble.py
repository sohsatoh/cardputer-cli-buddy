"""buddy_ble の RX 行組み立てを、bluetooth / micropython の偽物で検証する。"""

import pathlib
import sys
import time
import types

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))


class FakeBLE:
    def __init__(self):
        self.pending = b""
        self.handler = None

    def active(self, *a):
        return True

    def config(self, *a, **kw):
        return (0, bytes(6)) if a == ("mac",) else None

    def gatts_register_services(self, _):
        return ((1, 2),)

    def irq(self, h):
        self.handler = h

    def gap_advertise(self, *a, **kw):
        pass

    def gatts_set_buffer(self, *a):
        pass

    def gatts_read(self, _h):
        out, self.pending = self.pending, b""
        return out

    def write(self, data):
        self.pending += data
        self.handler(3, (0, 1))


@pytest.fixture
def ble(monkeypatch):
    fake = FakeBLE()
    monkeypatch.setitem(sys.modules, "bluetooth", types.SimpleNamespace(UUID=lambda s: s.encode(), BLE=lambda: fake))
    monkeypatch.setitem(sys.modules, "micropython", types.SimpleNamespace(const=lambda x: x, schedule=lambda f, a: None))
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.delitem(sys.modules, "buddy_ble", raising=False)
    import buddy_ble

    lines = []
    buddy_ble.BuddyBLE(on_line=lines.append)
    fake.handler(1, (0, 0, b""))  # connect
    return fake, lines


def test_split_writes_are_joined(ble):
    fake, lines = ble
    line = b"A" * 400
    for i in range(0, len(line), 180):
        fake.write(line[i : i + 180])
    assert lines == []
    fake.write(b"\nBB")
    fake.write(b"\n")
    assert lines == [line, b"BB"]


def test_overlong_line_discarded(ble):
    fake, lines = ble
    for _ in range(30):
        fake.write(b"C" * 180)  # 5400 byte, 改行なし
    fake.write(b"tail\nok\n")
    assert lines == [b"ok"]
    fake.write(b"D" * 4097 + b"\nE" * 1 + b"\n")
    assert lines == [b"ok", b"E"]
    fake.write(b"F" * 4096 + b"\n")
    assert lines[-1] == b"F" * 4096
