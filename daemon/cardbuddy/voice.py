"""音声入力の録音の組み立て（純粋ロジック）と WAV への書き出し。"""

import logging
import os
import sys
import tempfile
import wave
from array import array
from pathlib import Path

log = logging.getLogger(__name__)

RATE = 16000
CHUNK = 1600  # 1 フレームの μ-law サンプル数（100ms）
MAX_BYTES = 60 * RATE
SILENCE = 0xFF  # μ-law の 0


def _ulaw_decode(b: int) -> int:
    b = ~b & 0xFF
    s = (((b & 0x0F) << 3) + 0x84) << ((b >> 4) & 0x07)
    s -= 0x84
    return -s if b & 0x80 else s


# Python 3.13 で audioop が stdlib から消えたので表を自前で持つ
ULAW = [_ulaw_decode(b) for b in range(256)]


def ulaw_to_pcm16(data: bytes) -> bytes:
    pcm = array("h", map(ULAW.__getitem__, data))
    if sys.byteorder == "big":
        pcm.byteswap()
    return pcm.tobytes()


def write_wav(d: Path, ulaw: bytes) -> Path:
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    fd, name = tempfile.mkstemp(suffix=".wav", dir=d)  # mkstemp は 0600 で作る
    try:
        with os.fdopen(fd, "wb") as f, wave.open(f, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(ulaw_to_pcm16(ulaw))
    except BaseException:
        os.unlink(name)
        raise
    return Path(name)


class Recorder:
    def __init__(self):
        self.reset()

    def reset(self):
        self.vid: str | None = None
        self.lang = ""
        self.buf = bytearray()
        self.next = 0

    def begin(self, vid: str, lang: str):
        if self.vid is not None:
            log.info("voice %s superseded by %s", self.vid, vid)
        self.reset()
        self.vid, self.lang = vid, lang

    def audio(self, pt: bytes):
        if self.vid is None or len(pt) < 2 or len(pt) - 2 > CHUNK:
            log.debug("drop audio frame (%d bytes)", len(pt))
            return
        seq = int.from_bytes(pt[:2], "big")
        # ponytail: 欠けた seq は 1 フレーム分（CHUNK）の無音とみなす、デバイスが末尾以外を満杯で送る前提
        if seq < self.next or seq * CHUNK >= MAX_BYTES:
            log.debug("drop audio frame seq=%d", seq)
            return
        self.buf += bytes([SILENCE]) * ((seq - self.next) * CHUNK)
        self.buf += pt[2:]
        del self.buf[MAX_BYTES:]
        self.next = seq + 1

    def end(self, vid: str) -> tuple[bytes, str] | None:
        if vid != self.vid:
            return None
        out = (bytes(self.buf), self.lang)
        self.reset()
        return out

    def cancel(self, vid: str):
        if vid == self.vid:
            self.reset()
