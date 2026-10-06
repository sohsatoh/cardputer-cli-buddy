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
        self.sent = []  # notify したチャンク
        self.fail = []  # 次の notify から順に投げる errno（None なら成功）

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

    def gatts_notify(self, conn, handle, data):
        if self.fail:
            err = self.fail.pop(0)
            if err is not None:
                raise OSError(err)
        self.sent.append(bytes(data))

    def write(self, data):
        self.pending += data
        self.handler(3, (0, 1))


@pytest.fixture
def ble(monkeypatch):
    fake = FakeBLE()
    monkeypatch.setitem(sys.modules, "bluetooth", types.SimpleNamespace(UUID=lambda s: s.encode(), BLE=lambda: fake))
    monkeypatch.setitem(sys.modules, "micropython", types.SimpleNamespace(const=lambda x: x, schedule=lambda f, a: None))
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.setattr(time, "ticks_ms", lambda: 0, raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.delitem(sys.modules, "buddy_ble", raising=False)
    import buddy_ble

    lines = []
    link = buddy_ble.BuddyBLE(on_line=lines.append)
    fake.handler(1, (0, 0, b""))  # connect
    fake.link = link
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


def test_chunks_follow_negotiated_mtu(ble):
    fake, _ = ble
    link = fake.link
    assert link.send_line(b"x" * 50)
    assert [len(c) for c in fake.sent] == [20, 20, 11]
    fake.sent.clear()
    fake.handler(21, (0, 256))  # MTU exchanged
    assert link.send_line(b"y" * 600)
    assert [len(c) for c in fake.sent] == [253, 253, 95]
    fake.handler(1, (0, 0, b""))  # 再接続で 23 に戻る
    fake.sent.clear()
    assert link.send_line(b"z" * 30)
    assert [len(c) for c in fake.sent] == [20, 11]


def test_enomem_resends_the_same_chunk(ble):
    fake, _ = ble
    fake.fail = [None, 12, 12, None]
    assert fake.link.send_line(b"a" * 20 + b"b" * 20 + b"c" * 5)
    assert b"".join(fake.sent) == b"a" * 20 + b"b" * 20 + b"c" * 5 + b"\n"


def test_pump_is_non_blocking_and_keeps_line_order(ble):
    fake, _ = ble
    link = fake.link
    fake.fail = [None, 12]
    link.enqueue(b"A" * 45 + b"\n")
    assert link.pump() is False and not link.tx_idle()  # ENOMEM で次の周回へ
    assert link.pump() is True and link.tx_idle()
    fake.fail = [12] * 3 + [None]
    link.enqueue(b"B" * 30 + b"\n")
    assert link.pump() is False
    assert link.send_line(b"json")  # 後から送る行は、前の行を送り切ってから
    assert b"".join(fake.sent) == b"A" * 45 + b"\n" + b"B" * 30 + b"\n" + b"json\n"


def test_other_errors_and_disconnect_drop_the_queue(ble):
    fake, _ = ble
    link = fake.link
    fake.fail = [5]
    assert link.send_line(b"x") is False and link.tx_idle()
    fake.fail = [12]
    link.enqueue(b"y" * 30 + b"\n")
    assert link.pump() is False
    fake.handler(2, (0, 0, b""))  # disconnect
    assert link.pump() is None and link.tx_idle()
    fake.handler(1, (0, 0, b""))
    fake.fail = [12]
    link.enqueue(b"z" * 30 + b"\n")
    assert link.pump() is False
    fake.handler(2, (0, 0, b""))
    fake.handler(1, (0, 0, b""))  # 送りかけの行は新しい接続へ持ち越さない
    fake.sent.clear()
    assert link.send_line(b"hello")
    assert fake.sent == [b"hello\n"]


def test_send_line_keeps_line_queued_after_timeout(ble, monkeypatch):
    fake, _ = ble
    link = fake.link
    clock = [0]
    monkeypatch.setattr(time, "ticks_ms", lambda: clock[0], raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.setattr(time, "sleep_ms", lambda ms: clock.__setitem__(0, clock[0] + 100), raising=False)
    fake.fail = [12] * 100
    assert link.send_line(b"q" * 30)  # 送りかけを捨てると host 側の行が壊れるので、キューに残して後で送る
    assert not link.tx_idle()
    fake.fail = []
    assert link.pump() is True
    assert b"".join(fake.sent) == b"q" * 30 + b"\n"
