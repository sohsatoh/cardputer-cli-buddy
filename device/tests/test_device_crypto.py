import json
import os
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "device"))
sys.path.insert(0, str(ROOT / "daemon"))

import crypto as c  # noqa: E402
from cardbuddy import crypto as host  # noqa: E402

V = json.loads((ROOT / "testvectors/envelope.json").read_text())
KEY, NH, ND = (bytes.fromhex(V[k]) for k in ("key", "nh", "nd"))


def sessions():
    enc, mac = c.hkdf(KEY, NH, ND)
    return c.Session(enc, mac, c.DIR_H2D), c.Session(enc, mac, c.DIR_D2H)


def test_hkdf():
    enc, mac = c.hkdf(KEY, NH, ND)
    assert enc.hex() == V["enc_key"] and mac.hex() == V["mac_key"]


def test_hmac_matches_stdlib():
    import hashlib
    import hmac

    for key in (b"", b"k", bytes(64), bytes(range(100))):
        for msg in (b"", b"abc", bytes(200)):
            assert c.hmac_sha256(key, msg) == hmac.new(key, msg, hashlib.sha256).digest()


def test_hello():
    assert c.hello(c.ROLE_HOST, NH).decode().rstrip("\n") == V["hello_host"]
    assert c.hello_device(KEY, NH, ND).decode().rstrip("\n") == V["hello_device"]
    ack = c.decode_line(V["hello_ack"].encode())
    assert ack[:3] == bytes([c.VER, c.HELLO, c.ROLE_ACK])
    assert ack[3:] == c.hello_tag(KEY, b"h", NH, ND)
    assert c.hello_tag(KEY, b"h", NH, ND) == host.hello_tag(KEY, b"h", NH, ND)


def test_frames_seal_and_open():
    h, d = sessions()
    for f in V["frames"]:
        tx, rx = (h, d) if f["dir"] == c.DIR_H2D else (d, h)
        assert tx.seal_bytes(f["pt"].encode()).decode().rstrip("\n") == f["line"]
        assert rx.open(f["line"].encode()) == json.loads(f["pt"])


@pytest.mark.parametrize("case", V["reject_h2d"], ids=lambda x: x["why"])
def test_device_rejects(case):
    _, d = sessions()
    for f in V["frames"]:
        if f["dir"] == c.DIR_H2D:
            d.open(f["line"].encode())
    with pytest.raises(c.FrameError):
        d.open(case["line"].encode())


@pytest.mark.parametrize("n", [0, 1, 15, 16, 17, 100, 2047, 2048])
def test_cross_check_with_host(n):
    enc, mac = c.hkdf(KEY, NH, ND)
    assert (enc, mac) == host.hkdf(KEY, NH, ND)
    pt = os.urandom(n)
    for d in (c.DIR_H2D, c.DIR_D2H):
        dev, ref = c.Session(enc, mac, d), host.Session(enc, mac, d)
        for _ in range(3):
            assert dev.seal_bytes(pt) == ref.seal_bytes(pt)


def test_cross_open_host_frames():
    enc, mac = c.hkdf(KEY, NH, ND)
    ref = host.Session(enc, mac, c.DIR_H2D)
    dev = c.Session(enc, mac, c.DIR_D2H)
    msg = {"t": "perm", "hint": "日本語" * 100}
    assert dev.open(ref.seal(msg)) == msg
    back = {"t": "prompt", "text": "y" * 500}
    assert ref.open(dev.seal(back)) == back


def test_failed_json_does_not_advance_ctr():
    h, d = sessions()
    bad = h.seal_bytes(b"not json")
    with pytest.raises(c.FrameError):
        d.open(bad)
    assert d.rx_ctr == 0
    assert d.open(h.seal({"t": "ok"})) == {"t": "ok"}


def test_plaintext_limit():
    h, _ = sessions()
    with pytest.raises(c.FrameError):
        h.seal_bytes(b"x" * 2049)


def test_line_limit():
    _, d = sessions()
    with pytest.raises(c.FrameError):
        d.open(b"A" * 4097)


def test_audio_frame_matches_vector():
    h, d = sessions()
    for f in V["frames"]:
        tx, rx = (h, d) if f["dir"] == c.DIR_H2D else (d, h)
        tx.seal_bytes(f["pt"].encode())
    for f in V["audio_frames"]:
        assert d.tx_ctr + 1 == f["ctr"]
        assert d.seal_bytes(bytes.fromhex(f["pt_hex"]), c.AUDIO).decode().rstrip("\n") == f["line"]


def test_audio_frame_opens_on_host():
    enc, mac = c.hkdf(KEY, NH, ND)
    dev, ref = c.Session(enc, mac, c.DIR_D2H), host.Session(enc, mac, c.DIR_D2H)
    pt = bytes([0, 7]) + os.urandom(1600)
    line = dev.seal_bytes(pt, c.AUDIO)
    assert host.Session(enc, mac, c.DIR_H2D).open_frame(line) == (host.AUDIO, pt)
    assert ref.seal_bytes(pt, host.AUDIO) == line


def test_seal_accepts_buffers_without_copy():
    enc, mac = c.hkdf(KEY, NH, ND)
    pt = bytes([0, 9]) + os.urandom(1600)
    a = c.Session(enc, mac, c.DIR_D2H).seal_bytes(pt, c.AUDIO)
    buf = bytearray(b"xx" + pt + b"yy")
    b = c.Session(enc, mac, c.DIR_D2H).seal_bytes(memoryview(buf)[2:-2], c.AUDIO)
    assert a == b and a.endswith(b"\n") and a.count(b"\n") == 1


def test_hmac_pads_are_reused_per_session():
    enc, mac = c.hkdf(KEY, NH, ND)
    s = c.Session(enc, mac, c.DIR_D2H)
    assert s._mac(b"abc") == c.hmac_sha256(mac, b"abc")
    assert s._mac(memoryview(b"xabc")[1:]) == c.hmac_sha256(mac, b"abc")


@pytest.mark.parametrize("n", [0, 1, 2, 3, 16, 17, 1602, 2048])
def test_seal_into_matches_seal_bytes(n):
    enc, mac = c.hkdf(KEY, NH, ND)
    pt = os.urandom(n)
    want = c.Session(enc, mac, c.DIR_D2H).seal_bytes(pt, c.AUDIO)
    bufs = c.seal_buffers(n)
    s = c.Session(enc, mac, c.DIR_D2H)
    got = s.seal_into(memoryview(bytearray(pt)), c.AUDIO, bufs)
    assert bytes(got) == want
    ref = c.Session(enc, mac, c.DIR_D2H)
    ref.seal_bytes(pt, c.AUDIO)
    assert bytes(s.seal_into(pt, c.AUDIO, bufs)) == ref.seal_bytes(pt, c.AUDIO)  # バッファを使い回しても同じ


class _ZeroingAes:
    """IDF ヒープが尽きたときの AES。失敗すると出力を 0 で埋めて返す（実機で、平文のまま MAC の通る行が出た）。"""

    def __init__(self, key, mode):
        pass

    def encrypt(self, data, out=None):
        out = bytearray(len(data)) if out is None else out
        out[:] = bytes(len(data))
        return out


def _broken_aes(real, from_block, how):
    """from_block 番目以降のブロックで失敗する AES。how は zero（0 で埋める）か same（カウンタのまま返す）。"""

    class Aes:
        def __init__(self, key, mode):
            self._a = real(key, mode)

        def encrypt(self, data, out=None):
            src = bytes(data)
            r = self._a.encrypt(data, out)
            o = 16 * from_block
            r[o:] = bytes(len(src) - o) if how == "zero" else src[o:]
            return r

    return Aes


@pytest.mark.parametrize("how", ["zero", "same"])
@pytest.mark.parametrize("from_block", [0, 1, 3])
def test_seal_refuses_partly_failed_keystream(monkeypatch, from_block, how):
    _, dev = sessions()
    monkeypatch.setattr(c, "aes", _broken_aes(c.aes, from_block, how))
    with pytest.raises(c.FrameError):
        dev.seal({"t": "voice_cancel", "vid": "55f83072", "pad": "x" * 60})  # 5 ブロック以上
    with pytest.raises(c.FrameError):
        dev.seal_into(b"\x00\x01" + bytes(100), c.AUDIO, c.seal_buffers(102))


def test_seal_refuses_when_aes_returns_no_keystream(monkeypatch):
    _, dev = sessions()
    monkeypatch.setattr(c, "aes", _ZeroingAes)
    msg = {"t": "voice_cancel", "vid": "55f83072"}
    with pytest.raises(c.FrameError):
        dev.seal(msg)
    with pytest.raises(c.FrameError):
        dev.seal_into(b"\x00\x01" + bytes(100), c.AUDIO, c.seal_buffers(102))
    monkeypatch.undo()
    assert host.Session(*c.hkdf(KEY, NH, ND), c.DIR_H2D).open(dev.seal(msg)) == msg  # 次の行は通る
