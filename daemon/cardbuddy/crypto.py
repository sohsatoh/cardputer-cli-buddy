"""PROTOCOL.md の frame / セッション鍵の実装（ホスト側）。"""

import base64
import hashlib
import hmac
import json
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

VER = 0x01
HELLO = 0x48  # 'H'
DATA = 0x44  # 'D'
AUDIO = 0x41  # 'A'
ROLE_HOST = 0x68  # 'h'
ROLE_DEVICE = 0x64  # 'd'
ROLE_ACK = 0x6B  # 'k'
DIR_H2D = 0x01
DIR_D2H = 0x02
INFO = b"cardbuddy v1"
MAX_LINE = 4096
MAX_PLAINTEXT = 2048


class FrameError(Exception):
    pass


def hkdf(key: bytes, nh: bytes, nd: bytes) -> tuple[bytes, bytes]:
    prk = hmac.new(nh + nd, key, hashlib.sha256).digest()
    okm, t = b"", b""
    for i in (1, 2):
        t = hmac.new(prk, t + INFO + bytes([i]), hashlib.sha256).digest()
        okm += t
    return okm[:16], okm[16:48]


def hello(role: int, nonce: bytes) -> bytes:
    return base64.b64encode(bytes([VER, HELLO, role]) + nonce) + b"\n"


def parse_hello(line: bytes, want_role: int) -> bytes:
    raw = _b64(line)
    if len(raw) != 19 or raw[0] != VER or raw[1] != HELLO or raw[2] != want_role:
        raise FrameError("bad hello")
    return raw[3:]


def hello_tag(key: bytes, label: bytes, nh: bytes, nd: bytes) -> bytes:
    return hmac.new(key, b"cardbuddy v1 hello " + label + nh + nd, hashlib.sha256).digest()[:16]


def hello_device(key: bytes, nh: bytes, nd: bytes) -> bytes:
    return base64.b64encode(bytes([VER, HELLO, ROLE_DEVICE]) + nd + hello_tag(key, b"d", nh, nd)) + b"\n"


def parse_hello_device(line: bytes, key: bytes, nh: bytes) -> bytes:
    """Hello(d) の tag_d を検証して nd を返す。"""
    raw = _b64(line)
    if len(raw) != 35 or raw[0] != VER or raw[1] != HELLO or raw[2] != ROLE_DEVICE:
        raise FrameError("bad hello")
    nd = raw[3:19]
    if not hmac.compare_digest(raw[19:], hello_tag(key, b"d", nh, nd)):
        raise FrameError("bad hello tag")
    return nd


def hello_ack(key: bytes, nh: bytes, nd: bytes) -> bytes:
    return base64.b64encode(bytes([VER, HELLO, ROLE_ACK]) + hello_tag(key, b"h", nh, nd)) + b"\n"


def check_hello_ack(line: bytes, key: bytes, nh: bytes, nd: bytes) -> None:
    raw = _b64(line)
    if len(raw) != 19 or raw[0] != VER or raw[1] != HELLO or raw[2] != ROLE_ACK:
        raise FrameError("bad hello ack")
    if not hmac.compare_digest(raw[3:], hello_tag(key, b"h", nh, nd)):
        raise FrameError("bad hello ack tag")


def _b64(line: bytes) -> bytes:
    line = line.rstrip(b"\n")
    if len(line) > MAX_LINE:
        raise FrameError("line too long")
    try:
        return base64.b64decode(line, validate=True)
    except ValueError as e:
        raise FrameError("bad base64") from e


def _ctr(enc_key: bytes, d: int, ctr: int, data: bytes) -> bytes:
    icb = bytes([d]) + struct.pack(">I", ctr) + bytes(11)
    c = Cipher(algorithms.AES(enc_key), modes.CTR(icb)).encryptor()
    return c.update(data) + c.finalize()


class Session:
    """1 接続分のセッション。tx_dir は自分が送る向き。"""

    def __init__(self, enc_key: bytes, mac_key: bytes, tx_dir: int):
        self.enc_key, self.mac_key = enc_key, mac_key
        self.tx_dir = tx_dir
        self.rx_dir = DIR_D2H if tx_dir == DIR_H2D else DIR_H2D
        self.tx_ctr = 0
        self.rx_ctr = 0

    def seal(self, msg: dict) -> bytes:
        return self.seal_bytes(json.dumps(msg, ensure_ascii=False, separators=(",", ":")).encode())

    def seal_bytes(self, pt: bytes, kind: int = DATA) -> bytes:
        if len(pt) > MAX_PLAINTEXT:
            raise FrameError("plaintext too long")
        self.tx_ctr += 1
        head = bytes([VER, kind, self.tx_dir]) + struct.pack(">I", self.tx_ctr)
        ct = _ctr(self.enc_key, self.tx_dir, self.tx_ctr, pt)
        tag = hmac.new(self.mac_key, head + ct, hashlib.sha256).digest()[:16]
        return base64.b64encode(head + ct + tag) + b"\n"

    def open(self, line: bytes) -> dict:
        kind, msg = self.open_frame(line)
        if kind != DATA:
            raise FrameError("not a data frame")
        return msg

    def open_frame(self, line: bytes) -> tuple[int, dict | bytes]:
        """Data なら parse した dict、Audio なら平文の bytes を返す。"""
        raw = _b64(line)
        if len(raw) < 7 + 16 or raw[0] != VER or raw[1] not in (DATA, AUDIO):
            raise FrameError("bad frame")
        if raw[2] != self.rx_dir:
            raise FrameError("wrong direction")
        head, ct, tag = raw[:7], raw[7:-16], raw[-16:]
        want = hmac.new(self.mac_key, head + ct, hashlib.sha256).digest()[:16]
        if not hmac.compare_digest(tag, want):
            raise FrameError("bad tag")
        (ctr,) = struct.unpack(">I", head[3:7])
        if ctr <= self.rx_ctr:
            raise FrameError("replay")
        pt = _ctr(self.enc_key, self.rx_dir, ctr, ct)
        if raw[1] == AUDIO:
            self.rx_ctr = ctr
            return AUDIO, pt
        try:
            msg = json.loads(pt)
        except ValueError as e:
            raise FrameError("bad json") from e
        if not isinstance(msg, dict):
            raise FrameError("not an object")
        self.rx_ctr = ctr
        return DATA, msg
