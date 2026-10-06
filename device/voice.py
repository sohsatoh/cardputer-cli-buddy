"""音声入力の録音と送信（PROTOCOL.md の「音声入力」）。

M5.Mic で 16kHz の int16 を 100ms ずつ録り、μ-law にして RAM のリングに積む。
送信はメインループから service() でノンブロッキングに行う。リンクが一時的に遅くなっても
録音を止めないためで、リングがあふれたら古いフレームを捨てる（host は seq の欠けを無音で埋める）。
"""

import time

import M5

import buddy_protocol

RATE = 16000
SAMPLES = 1600  # 100ms
FRAME = 2 + SAMPLES  # seq(2) + μ-law
SLOTS = 6  # 約 0.6 秒分、マイク 2 面（6.4KB）と合わせて 16KB に収める
MAX_FRAMES = 600  # 60 秒

try:
    import micropython

    @micropython.viper
    def _ulaw(src: ptr8, dst: ptr8, n: int):  # noqa: F821
        for i in range(n):
            s = int(src[2 * i]) | (int(src[2 * i + 1]) << 8)
            if s & 0x8000:
                s = s - 0x10000
            sign = 0
            if s < 0:
                s = 0 - s
                sign = 0x80
            if s > 32635:
                s = 32635
            s += 0x84
            e = 7
            m = 0x4000
            while e > 0 and (s & m) == 0:
                e -= 1
                m = m >> 1
            dst[i] = (0xFF ^ (sign | (e << 4) | ((s >> (e + 3)) & 0x0F))) & 0xFF

except Exception:  # CPython（テスト）には viper が無い

    def _ulaw(src, dst, n):
        for i in range(n):
            s = src[2 * i] | (src[2 * i + 1] << 8)
            if s & 0x8000:
                s -= 0x10000
            sign = 0
            if s < 0:
                s = -s
                sign = 0x80
            if s > 32635:
                s = 32635
            s += 0x84
            e = 7
            m = 0x4000
            while e > 0 and not (s & m):
                e -= 1
                m >>= 1
            dst[i] = 0xFF ^ (sign | (e << 4) | ((s >> (e + 3)) & 0x0F))


def _peak(buf):
    p = 0
    for i in range(0, len(buf), 32):
        s = buf[i] | (buf[i + 1] << 8)
        if s & 0x8000:
            s = 0x10000 - s
        if s > p:
            p = s
    return p


class Voice:
    """state: idle / rec（録音中）/ flush（停止、残りを送信中）/ wait（認識待ち）/ lost（切断で中断）"""

    def __init__(self, proto, slots=SLOTS):
        self.p = proto
        self.state = "idle"
        self.vid = None
        self.level = 0
        self.dropped = 0
        self._bufs = (bytearray(2 * SAMPLES), bytearray(2 * SAMPLES))
        self._ring = bytearray(slots * FRAME)
        self._slots = slots
        self._mic_on = False
        self._sess = None
        self._reset()

    def _reset(self):
        self._head = 0
        self._count = 0
        self._sub = 0
        self._done = 0

    @property
    def elapsed_ms(self):
        return self._done * 100

    def start(self, lang):
        vid = buddy_protocol.new_vid()
        if not self.p.voice_begin(vid, lang):
            return False
        self.vid, self._sess = vid, self.p.session
        self._reset()
        self.level = self.dropped = 0
        M5.Speaker.end()
        M5.Mic.begin()
        self._mic_on = True
        for _ in range(2):
            self._record()
        self.state = "rec"
        return True

    def _record(self):
        M5.Mic.record(self._bufs[self._sub % 2], RATE)
        self._sub += 1

    def stop(self):
        if self.state == "rec":
            self.state = "flush"

    def cancel(self):
        if self.state in ("rec", "flush", "wait"):
            self._mic_off()
            self._count = 0
            if self.p.session is self._sess:
                self.p.voice_cancel(self.vid)
        self.state = "idle"

    def done(self):
        self.state = "idle"

    def close(self):
        self._mic_off()

    def _mic_off(self):
        if not self._mic_on:
            return
        # 待ちのバッファを残して end してよいかは未確認なので、終わるまで待つ（最大 200ms）
        for _ in range(200):
            if not M5.Mic.isRecording():
                break
            time.sleep_ms(1)
        M5.Mic.end()
        M5.Speaker.begin()
        self._mic_on = False

    def _put(self, src):
        if self._count == self._slots:
            self._head = (self._head + 1) % self._slots
            self._count -= 1
            self.dropped += 1
        i = (self._head + self._count) % self._slots * FRAME
        seq = self._done
        self._ring[i] = seq >> 8 & 0xFF
        self._ring[i + 1] = seq & 0xFF
        _ulaw(src, memoryview(self._ring)[i + 2 : i + FRAME], SAMPLES)
        self._count += 1
        self.level = _peak(src)

    def _harvest(self):
        fin = self._sub - M5.Mic.isRecording()
        while self._done < fin:
            self._put(self._bufs[self._done % 2])
            self._done += 1
            if self.state == "rec" and self._sub < MAX_FRAMES and self._sub - self._done < 2:
                self._record()
        if self._done >= MAX_FRAMES:
            self.stop()

    def service(self, link):
        """メインループから毎回呼ぶ。link は tx_idle() / enqueue(line) を持つ送信キュー。"""
        if self.state in ("rec", "flush", "wait") and self.p.session is not self._sess:
            self._mic_off()
            self._count = 0
            self.state = "lost"
            return
        if self._mic_on:
            self._harvest()
            if self.state != "rec" and not M5.Mic.isRecording():
                self._mic_off()
        while self._count and link.tx_idle():
            i = self._head * FRAME
            line = self.p.seal_audio(bytes(self._ring[i : i + FRAME]))
            if line:
                link.enqueue(line)
            self._head = (self._head + 1) % self._slots
            self._count -= 1
        if self.state == "flush" and not self._mic_on and not self._count:
            self.p.voice_end(self.vid)
            self.state = "wait"
