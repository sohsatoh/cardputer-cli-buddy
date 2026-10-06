"""PROTOCOL.md の frame / セッション鍵の実装（デバイス側、MicroPython）。

ホストの参照実装 daemon/cardbuddy/crypto.py と同じ API・同じ出力にする。
MicroPython には hmac も AES-CTR も無い前提で、hashlib.sha256 と AES-ECB だけで組み立てる。
"""

import hashlib
import json
import struct

try:
    import ubinascii as binascii
except ImportError:
    import binascii

try:
    from ucryptolib import aes
except ImportError:
    try:
        from cryptolib import aes
    except ImportError:
        # ホストの CPython でテストするためだけのシムで、実機では使われない
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        class aes:
            def __init__(self, key, mode):
                self._e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()

            def encrypt(self, data):
                return self._e.update(data)


VER = 0x01
HELLO = 0x48  # 'H'
DATA = 0x44  # 'D'
AUDIO = 0x41  # 'A'
ROLE_HOST = 0x68  # 'h'
ROLE_DEVICE = 0x64  # 'd'
DIR_H2D = 0x01
DIR_D2H = 0x02
INFO = b"cardbuddy v1"
MAX_LINE = 4096
MAX_PLAINTEXT = 2048
_ECB = 1


class FrameError(Exception):
    pass


def hmac_sha256(key, msg):
    if len(key) > 64:
        key = hashlib.sha256(key).digest()
    key = key + bytes(64 - len(key))
    inner = hashlib.sha256(bytes(b ^ 0x36 for b in key))
    inner.update(msg)
    outer = hashlib.sha256(bytes(b ^ 0x5C for b in key))
    outer.update(inner.digest())
    return outer.digest()


def hkdf(key, nh, nd):
    prk = hmac_sha256(nh + nd, key)
    t1 = hmac_sha256(prk, INFO + b"\x01")
    t2 = hmac_sha256(prk, t1 + INFO + b"\x02")
    okm = t1 + t2
    return okm[:16], okm[16:48]


def hello(role, nonce):
    return _b64enc(bytes([VER, HELLO, role]) + nonce)


def decode_line(line):
    """1 行（`\\n` 有無どちらでも）を frame の bytes に戻す。"""
    line = line.rstrip(b"\n")
    if len(line) > MAX_LINE:
        raise FrameError("line too long")
    try:
        return binascii.a2b_base64(line)
    except ValueError:
        raise FrameError("bad base64")


def _b64enc(raw):
    return binascii.b2a_base64(raw).rstrip(b"\n") + b"\n"


def _ctr(enc_key, d, ctr, data):
    n = (len(data) + 15) // 16
    if not n:
        return b""
    blk = bytearray(16 * n)
    head = bytes([d]) + struct.pack(">I", ctr) + bytes(7)
    for i in range(n):
        blk[16 * i : 16 * i + 12] = head
        struct.pack_into(">I", blk, 16 * i + 12, i)
    ks = aes(enc_key, _ECB).encrypt(blk)
    size = len(data)
    # 1 byte ずつの XOR より、多倍長整数 1 回の XOR の方が実機で 2 倍以上速い
    return (int.from_bytes(data, "big") ^ int.from_bytes(ks[:size], "big")).to_bytes(size, "big")


def _eq(a, b):
    if len(a) != len(b):
        return False
    r = 0
    for x, y in zip(a, b):
        r |= x ^ y
    return r == 0


class Session:
    """1 接続分のセッション。tx_dir は自分が送る向き。"""

    def __init__(self, enc_key, mac_key, tx_dir):
        self.enc_key, self.mac_key = enc_key, mac_key
        self.tx_dir = tx_dir
        self.rx_dir = DIR_D2H if tx_dir == DIR_H2D else DIR_H2D
        self.tx_ctr = 0
        self.rx_ctr = 0

    def seal(self, msg):
        try:
            s = json.dumps(msg, separators=(",", ":"))
        except TypeError:  # separators の無い MicroPython
            s = json.dumps(msg)
        return self.seal_bytes(s.encode())

    def seal_bytes(self, pt, kind=DATA):
        if len(pt) > MAX_PLAINTEXT:
            raise FrameError("plaintext too long")
        self.tx_ctr += 1
        head = bytes([VER, kind, self.tx_dir]) + struct.pack(">I", self.tx_ctr)
        ct = _ctr(self.enc_key, self.tx_dir, self.tx_ctr, pt)
        tag = hmac_sha256(self.mac_key, head + ct)[:16]
        return _b64enc(head + ct + tag)

    def open(self, line):
        return self.open_raw(decode_line(line))

    def open_raw(self, raw):
        if len(raw) < 7 + 16 or raw[0] != VER or raw[1] != DATA:
            raise FrameError("bad frame")
        if raw[2] != self.rx_dir:
            raise FrameError("wrong direction")
        head, ct, tag = raw[:7], raw[7:-16], raw[-16:]
        if not _eq(tag, hmac_sha256(self.mac_key, head + ct)[:16]):
            raise FrameError("bad tag")
        ctr = struct.unpack(">I", head[3:7])[0]
        if ctr <= self.rx_ctr:
            raise FrameError("replay")
        pt = _ctr(self.enc_key, self.rx_dir, ctr, ct)
        try:
            msg = json.loads(pt.decode())
        except ValueError:  # UnicodeError も ValueError の派生
            raise FrameError("bad json")
        if not isinstance(msg, dict):
            raise FrameError("not an object")
        self.rx_ctr = ctr
        return msg
