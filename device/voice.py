"""音声入力の録音と送信（PROTOCOL.md の「音声入力」）。

M5.Mic で 16kHz の int16 を 100ms ずつ録り、μ-law にして flash のファイル（SPOOL）に追記する。
送信はメインループから service() でノンブロッキングに、ファイルの先頭から順に行う。
Wi-Fi では送信が録音に追いつかないことがあり、RAM のリングであふれたフレームを捨てると
認識できなくなった（実機）ので、フレームは捨てずに flash に置き、止めた後も送り切ってから voice_end を送る。
ファイルは送り終えたとき・取り消し・中断・起動時に消す。

バッファは起動時（Wi-Fi の前）に小分けで確保して保持する。Wi-Fi の使用中に確保すると、
GC ヒープが IDF ヒープを奪って伸び、Wi-Fi / lwIP のメモリが尽きる（実機で確認）。
Speaker はアプリの起動時に止め、ここでは触らない（録音後に begin すると IDF ヒープを約 7.7KB 取り直す）。
"""

import os
import time

import M5

import buddy_protocol
import crypto

RATE = 16000
SAMPLES = 1600  # 100ms
FRAME = 2 + SAMPLES  # seq(2) + μ-law
MAX_FRAMES = 600  # 60 秒
SPOOL = "/flash/cardbuddy_voice.tmp"
BATCH = 2  # flash への 1 回の書き込みのフレーム数。1 つずつだと最大 55ms、2 つで 25ms（実機）
WAIT_MS = 60000  # voice_end を送ってから認識結果までの上限
FLUSH_PER_REC = 3  # 止めてから送り切るまでの上限は、録音の長さ × 3 + FLUSH_EXTRA_MS
FLUSH_EXTRA_MS = 10000

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


def _flash_free(path):
    st = os.statvfs(path.rsplit("/", 1)[0] or "/")
    return st[0] * st[3]


def _remove(path):
    try:
        os.remove(path)
    except OSError:
        pass


def alloc():
    """録音のバッファ（マイク 2 面、書き込み、読み出し、送信の行）を確保する。足りなければ None。

    アプリはこれを、他のモジュールを import する前（GC ヒープが断片化する前）に呼ぶ。
    大きい塊を断片化したヒープに置こうとすると、GC ヒープが IDF ヒープの最大ブロック（実機で約 32KB）を
    丸ごと取って伸び、Wi-Fi のメモリが尽きる。小分けにし、早い時点で確保すれば既存の空きに収まる。
    """
    try:
        # 送信の行（約 5.4KB）は enqueue が送信キューの空いたときだけなので、1 組を使い回せる
        return ((bytearray(2 * SAMPLES), bytearray(2 * SAMPLES)), bytearray(BATCH * FRAME), bytearray(FRAME),
                crypto.seal_buffers(FRAME))
    except MemoryError:
        return None


class Voice:
    """state: idle / rec（録音中）/ flush（停止、残りを送信中）/ wait（認識待ち）/ lost（中断）"""

    def __init__(self, proto, bufs=None, path=None):
        self.p = proto
        self.state = "idle"
        self.vid = None
        self.level = 0
        self.gaps = 0  # マイクのバッファが 2 面とも埋まって取りこぼした回数（診断用）
        self.err = None  # 録音できない・中断した理由。ヘッダの左に出すので、かな 10 文字ほどに収める
        self._path = path or SPOOL
        self._f = None
        self._mic_on = False
        self._sess = None
        self._t_stop = 0
        self._q_link = None  # 最後に音声の行を積んだリンク
        self._mic, self._wbuf, self._rbuf, self._tx = bufs or alloc() or (None, None, None, None)
        _remove(self._path)  # 前回の異常終了で残ったもの
        self._reset()

    def _reset(self):
        self._sub = 0
        self._done = 0  # 取り出したフレーム数（= 次の seq）
        self._n = 0  # 書き込み待ちで _wbuf にあるフレーム数
        self._written = 0
        self._sent = 0

    @property
    def elapsed_ms(self):
        return self._done * 100

    @property
    def progress(self):
        total = self._written + self._n
        return self._sent * 100 // total if total else 100

    def start(self, lang):
        self.err = None
        if self._mic is None:
            self.err = "メモリ不足で録音不可"
            return False
        try:
            if _flash_free(self._path) < MAX_FRAMES * FRAME:
                self.err = "flash の空き不足"
                return False
            self._f = open(self._path, "w+b")
        except OSError as e:
            self.err = "flash に書けません"
            print("voice: spool open failed:", e)
            return False
        vid = buddy_protocol.new_vid()
        if not self.p.voice_begin(vid, lang):
            self._close()
            self.err = "未接続で録音不可"
            return False
        self.vid, self._sess = vid, self.p.session
        self._reset()
        self.level = self.gaps = 0
        M5.Mic.begin()
        self._mic_on = True
        for _ in range(2):
            self._record()
        if not M5.Mic.isRecording():
            # I2S の DMA などに IDF ヒープが約 7KB 要り、足りないと黙って始まらない（実機）
            print("voice: mic did not start")
            self._mic_off()
            self.p.voice_cancel(vid)
            self._close()
            self.err = "メモリ不足で録音不可"
            return False
        self.state = "rec"
        return True

    def _record(self):
        M5.Mic.record(self._mic[self._sub % 2], RATE)
        self._sub += 1

    def stop(self):
        if self.state == "rec":
            self.state = "flush"
            self._t_stop = buddy_protocol.ms()
            print("voice: stop", self.elapsed_ms, "ms, gaps", self.gaps)

    def cancel(self):
        if self.state in ("rec", "flush", "wait"):
            self._mic_off()
            if self.p.session is self._sess:
                self.p.voice_cancel(self.vid)
        self._close()
        self.state = "idle"

    def _abort(self, err):
        print("voice: abort:", err)
        self.cancel()
        self.err = err
        self.state = "lost"

    def done(self):
        self.state = "idle"

    def close(self):
        self._mic_off()
        self._close()

    def _close(self):
        if self._f is not None:
            try:
                self._f.close()
            except OSError:
                pass
            self._f = None
        _remove(self._path)

    def _mic_off(self):
        if not self._mic_on:
            return
        # 待ちのバッファを残して end してよいかは未確認なので、終わるまで待つ（最大 200ms）
        for _ in range(200):
            if not M5.Mic.isRecording():
                break
            time.sleep_ms(1)
        M5.Mic.end()
        self._mic_on = False

    def _put(self, src):
        i = self._n * FRAME
        w = self._wbuf
        w[i] = self._done >> 8 & 0xFF
        w[i + 1] = self._done & 0xFF
        _ulaw(src, memoryview(w)[i + 2 : i + FRAME], SAMPLES)
        self._n += 1
        self.level = _peak(src)
        if self._n == BATCH:
            self._write()

    def _write(self):
        if not self._n:
            return
        self._f.seek(self._written * FRAME)
        self._f.write(memoryview(self._wbuf)[: self._n * FRAME])
        self._written += self._n
        self._n = 0

    def _harvest(self):
        pend = M5.Mic.isRecording()
        fin = self._sub - pend
        if pend == 0 and self.state == "rec" and self._sub < MAX_FRAMES and self._done < fin:
            self.gaps += 1
        while self._done < fin:
            self._put(self._mic[self._done % 2])
            self._done += 1
            if self.state == "rec" and self._sub < MAX_FRAMES and self._sub - self._done < 2:
                self._record()
        if self._done >= MAX_FRAMES:
            self.stop()

    def service(self, link):
        """メインループから毎回呼ぶ。link は tx_idle() / enqueue(line) を持つ送信キュー。"""
        st = self.state
        if st in ("rec", "flush", "wait") and self.p.session is not self._sess:
            self._abort("切断で音声を取り消し")
            return
        if st == "flush" and buddy_protocol.since(self._t_stop) > self.elapsed_ms * FLUSH_PER_REC + FLUSH_EXTRA_MS:
            self._abort("送り切れず取り消し")
            return
        if st == "wait" and buddy_protocol.since(self._t_stop) > WAIT_MS:
            self._abort("認識が時間切れ")
            return
        try:
            if self._mic_on:
                self._harvest()
                if self.state != "rec" and not M5.Mic.isRecording():
                    self._mic_off()
                    self._write()
            # Wi-Fi はマイクの動作中に送ると 68KB/s → 2〜12KB/s まで落ち、止めた後もしばらく戻らない（実機）。
            # 録音中は flash に溜めるだけにして、止めてから送る。BLE は録音中も送る
            hold = self.state == "rec" and getattr(link, "kind", "") == "Wi-Fi"
            # 1 回に 1 フレームだけ送る。残りは次の周回で、main loop の pump が送り終えてから
            # 行は共有バッファを指すので、前に積んだリンク（途中で替わることがある）が送り終えるまで次を作らない
            q = self._q_link
            if (not hold and self._f is not None and self._sent < self._written and link.tx_idle()
                    and (q is None or q.tx_idle())):
                self._f.seek(self._sent * FRAME)
                self._f.readinto(self._rbuf)
                line = self.p.seal_audio(self._rbuf, self._tx)
                if not line:
                    return  # 暗号化できなかった。同じフレームを次の周回で送り直す
                link.enqueue(line)
                self._q_link = link
                self._sent += 1
        except OSError as e:
            print("voice: spool error:", e)
            self._abort("flash の読み書き失敗")
            return
        if self.state == "flush" and not self._mic_on and not self._n and self._sent == self._written:
            self.p.voice_end(self.vid)
            print("voice: end sent after", buddy_protocol.since(self._t_stop), "ms,", self._sent, "frames")
            self._close()
            self._t_stop = buddy_protocol.ms()
            self.state = "wait"
