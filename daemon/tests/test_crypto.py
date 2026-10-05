import json
import pathlib

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cardbuddy import crypto as c

V = json.loads((pathlib.Path(__file__).parents[2] / "testvectors/envelope.json").read_text())
KEY, NH, ND = (bytes.fromhex(V[k]) for k in ("key", "nh", "nd"))


def test_hkdf_matches_rfc5869_impl():
    okm = HKDF(hashes.SHA256(), 48, NH + ND, c.INFO).derive(KEY)
    assert c.hkdf(KEY, NH, ND) == (okm[:16], okm[16:])
    assert okm[:16].hex() == V["enc_key"] and okm[16:].hex() == V["mac_key"]


def test_hello():
    assert c.parse_hello(V["hello_host"].encode(), c.ROLE_HOST) == NH
    assert c.parse_hello(V["hello_device"].encode(), c.ROLE_DEVICE) == ND
    with pytest.raises(c.FrameError):
        c.parse_hello(V["hello_host"].encode(), c.ROLE_DEVICE)


def sessions():
    enc, mac = c.hkdf(KEY, NH, ND)
    return c.Session(enc, mac, c.DIR_H2D), c.Session(enc, mac, c.DIR_D2H)


def test_frames_seal_and_open():
    host, dev = sessions()
    for f in V["frames"]:
        tx, rx = (host, dev) if f["dir"] == c.DIR_H2D else (dev, host)
        assert tx.seal_bytes(f["pt"].encode()).decode().rstrip("\n") == f["line"]
        assert rx.open(f["line"].encode()) == json.loads(f["pt"])


@pytest.mark.parametrize("case", V["reject_h2d"], ids=lambda c_: c_["why"])
def test_device_rejects(case):
    host, dev = sessions()
    for f in V["frames"]:
        if f["dir"] == c.DIR_H2D:
            dev.open(f["line"].encode())
    with pytest.raises(c.FrameError):
        dev.open(case["line"].encode())


def test_audio_frame_follows_data_counter():
    host, dev = sessions()
    for f in V["frames"]:
        tx, rx = (host, dev) if f["dir"] == c.DIR_H2D else (dev, host)
        rx.open(tx.seal_bytes(f["pt"].encode()))
    a = V["audio_frames"][0]
    pt = bytes.fromhex(a["pt_hex"])
    assert dev.seal_bytes(pt, c.AUDIO).decode().rstrip("\n") == a["line"]
    assert host.open_frame(a["line"].encode()) == (c.AUDIO, pt)
    with pytest.raises(c.FrameError):
        host.open_frame(a["line"].encode())  # replay
