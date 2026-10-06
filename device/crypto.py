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

            def encrypt(self, data, out=None):
                r = self._e.update(bytes(data))
                if out is None:
                    return r
                out[:] = r
                return out


_B64 = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"

try:
    import micropython

    @micropython.viper
    def _xor(dst: ptr8, src: ptr8, n: int):  # noqa: F821
        for i in range(n):
            dst[i] = dst[i] ^ src[i]

    @micropython.viper
    def _b64_into(src: ptr8, n: int, dst: ptr8, tbl: ptr8) -> int:  # noqa: F821
        i = 0
        j = 0
        while i + 2 < n:
            a = src[i]
            b = src[i + 1]
            c = src[i + 2]
            dst[j] = tbl[a >> 2]
            dst[j + 1] = tbl[((a & 3) << 4) | (b >> 4)]
            dst[j + 2] = tbl[((b & 15) << 2) | (c >> 6)]
            dst[j + 3] = tbl[c & 63]
            i += 3
            j += 4
        if n - i == 1:
            a = src[i]
            dst[j] = tbl[a >> 2]
            dst[j + 1] = tbl[(a & 3) << 4]
            dst[j + 2] = 61
            dst[j + 3] = 61
            j += 4
        elif n - i == 2:
            a = src[i]
            b = src[i + 1]
            dst[j] = tbl[a >> 2]
            dst[j + 1] = tbl[((a & 3) << 4) | (b >> 4)]
            dst[j + 2] = tbl[(b & 15) << 2]
            dst[j + 3] = 61
            j += 4
        dst[j] = 10
        return j + 1

except Exception:  # CPython（テスト）には viper が無い

    def _xor(dst, src, n):
        for i in range(n):
            dst[i] ^= src[i]

    def _b64_into(src, n, dst, tbl):
        b = binascii.b2a_base64(bytes(src[:n]))
        dst[: len(b)] = b
        return len(b)


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


def hello_tag(key, label, nh, nd):
    return hmac_sha256(key, b"cardbuddy v1 hello " + label + nh + nd)[:16]


def hello_device(key, nh, nd):
    return _b64enc(bytes([VER, HELLO, ROLE_DEVICE]) + nd + hello_tag(key, b"d", nh, nd))


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


def _ctr(enc_key, d, ctr, data, blk=None):
    """data を AES-CTR で暗号化（復号）した bytearray を返す（長さは 16 の倍数に切り上げ、先頭 len(data) が結果）。

    実機では音声 1 フレームごとに呼ぶので、一時オブジェクトを作らないよう、鍵ストリームを
    in-place で作って viper で XOR する（多倍長整数を使う版は 1 フレームで約 26KB を確保し、
    GC ヒープが IDF ヒープを奪って伸びる原因になった）。
    """
    n = (len(data) + 15) // 16
    if blk is None:
        blk = bytearray(16 * n)
    head = bytes([d]) + struct.pack(">I", ctr) + bytes(7)
    for i in range(n):
        blk[16 * i : 16 * i + 12] = head
        struct.pack_into(">I", blk, 16 * i + 12, i)
    if n:
        ks = memoryview(blk)[: 16 * n]
        aes(enc_key, _ECB).encrypt(ks, ks)
        _xor(blk, data, len(data))
    return blk


def seal_buffers(n):
    """平文 n byte までを seal_into で包むための作業バッファ（frame, 鍵ストリーム, 行）。"""
    size = 7 + n + 16
    return bytearray(size), bytearray(16 * ((n + 15) // 16)), bytearray(4 * ((size + 2) // 3) + 1)


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
        k = mac_key + bytes(64 - len(mac_key))
        self._ipad = bytes(b ^ 0x36 for b in k)
        self._opad = bytes(b ^ 0x5C for b in k)
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

    def _mac(self, msg):
        inner = hashlib.sha256(self._ipad)
        inner.update(msg)
        outer = hashlib.sha256(self._opad)
        outer.update(inner.digest())
        return outer.digest()

    def _seal(self, pt, kind, frame, blk):
        n = len(pt)
        if n > MAX_PLAINTEXT:
            raise FrameError("plaintext too long")
        self.tx_ctr += 1
        frame[0], frame[1], frame[2] = VER, kind, self.tx_dir
        struct.pack_into(">I", frame, 3, self.tx_ctr)
        mv = memoryview(frame)
        mv[7 : 7 + n] = memoryview(_ctr(self.enc_key, self.tx_dir, self.tx_ctr, pt, blk))[:n]
        mv[7 + n : 7 + n + 16] = self._mac(mv[: 7 + n])[:16]
        return mv[: 7 + n + 16]

    def seal_bytes(self, pt, kind=DATA):
        frame = self._seal(pt, kind, bytearray(7 + len(pt) + 16), None)
        line = binascii.b2a_base64(frame)
        return line if line[-1:] == b"\n" else line + b"\n"

    def seal_into(self, pt, kind, bufs):
        """seal_bytes と同じ行を、seal_buffers() の作業バッファに作って memoryview で返す。

        音声は 100ms ごとに seal するので、毎回 8KB ほど確保すると GC ヒープが伸び縮みして
        IDF ヒープを奪い、Wi-Fi のメモリが尽きる（実機）。返す行はバッファを指すので、次に呼ぶまでに送り終えること。
        """
        frame, blk, line = bufs
        f = self._seal(pt, kind, frame, blk)
        return memoryview(line)[: _b64_into(f, len(f), line, _B64)]

    def open(self, line):
        return self.open_raw(decode_line(line))

    def open_raw(self, raw):
        if len(raw) < 7 + 16 or raw[0] != VER or raw[1] != DATA:
            raise FrameError("bad frame")
        if raw[2] != self.rx_dir:
            raise FrameError("wrong direction")
        head, ct, tag = raw[:7], raw[7:-16], raw[-16:]
        if not _eq(tag, self._mac(head + ct)[:16]):
            raise FrameError("bad tag")
        ctr = struct.unpack(">I", head[3:7])[0]
        if ctr <= self.rx_ctr:
            raise FrameError("replay")
        pt = bytes(memoryview(_ctr(self.enc_key, self.rx_dir, ctr, ct))[: len(ct)])
        try:
            msg = json.loads(pt.decode())
        except ValueError:  # UnicodeError も ValueError の派生
            raise FrameError("bad json")
        if not isinstance(msg, dict):
            raise FrameError("not an object")
        self.rx_ctr = ctr
        return msg
