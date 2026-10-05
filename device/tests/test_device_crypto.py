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
    assert c.hello(c.ROLE_DEVICE, ND).decode().rstrip("\n") == V["hello_device"]


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
