"""voice（録音・μ-law・リング・送信）を、M5.Mic / Speaker と BLE の偽物で検証する。"""

import struct
import sys
import time
import types
import warnings

import pytest

from test_device_protocol import Dev, host


class FakeMic:
    def __init__(self, log):
        self.q = []
        self.log = log

    def begin(self):
        self.log.append("mic.begin")

    def end(self):
        assert not self.q, "録音中のバッファを残したまま end しない"
        self.log.append("mic.end")

    def record(self, buf, rate):
        assert rate == 16000 and len(buf) == 3200 and len(self.q) < 2  # M5Unified のキューは 2 面
        self.q.append(buf)

    def isRecording(self):
        return len(self.q)

    def finish(self, value=1000):
        buf = self.q.pop(0)
        struct.pack_into("<1600h", buf, 0, *([value] * 1600))


class FakeSpeaker:
    def __init__(self, log):
        self.log = log

    def begin(self):
        self.log.append("spk.begin")

    def end(self):
        self.log.append("spk.end")


class FakeLink:
    def __init__(self):
        self.idle = True
        self.lines = []

    def tx_idle(self):
        return self.idle

    def enqueue(self, line):
        # 音声の行は使い回しのバッファを指すので、本物のキューと同じく送り終えるまでの間だけ有効
        self.lines.append(bytes(line))


def ulaw_ref(v):
    sign = 0x80 if v < 0 else 0
    v = min(abs(v), 32635) + 0x84
    e = 7
    while e > 0 and not (v & (0x4000 >> (7 - e))):
        e -= 1
    return 0xFF ^ (sign | (e << 4) | ((v >> (e + 3)) & 0x0F))


@pytest.fixture
def env(monkeypatch, tmp_path):
    log = []
    m5 = types.SimpleNamespace(Mic=FakeMic(log), Speaker=FakeSpeaker(log))
    monkeypatch.setitem(sys.modules, "M5", m5)
    clock = [0]
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.setattr(time, "ticks_ms", lambda: clock[0], raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.delitem(sys.modules, "voice", raising=False)
    import voice

    voice.clock = clock
    monkeypatch.setattr(voice, "SPOOL", str(tmp_path / "voice.tmp"))

    d = Dev()
    d.handshake()
    v = voice.Voice(d.p)
    return voice, d, v, m5.Mic, log, FakeLink()


def audio(link):
    out = []
    rx = link.host
    for line in link.lines:
        kind, pt = rx.open_frame(line)
        assert kind == host.AUDIO and len(pt) == 1602
        out.append((pt[0] << 8 | pt[1], pt[2:]))
    link.lines.clear()
    return out


def start(d, v, link, lang="ja-JP"):
    assert v.start(lang)
    (msg,) = d.replies()
    assert msg == {"t": "voice_begin", "vid": v.vid, "lang": lang}
    link.host = d.host  # Audio と Data は同じ ctr の列なので、同じ受信側で開く
    return v.vid


def test_ulaw_known_values(env):
    voice = env[0]

    vals = [0, -8, 100, -1000, 32767, -32768, 5000, -1, 1, 31, -31, 32635, 32636, -32635, 4096, -4096]
    src = bytearray(2 * len(vals))
    struct.pack_into("<%dh" % len(vals), src, 0, *vals)
    dst = bytearray(len(vals))
    voice._ulaw(src, dst, len(vals))
    assert bytes(dst) == bytes(ulaw_ref(x) for x in vals)
    assert bytes(dst[:2]) == b"\xff\x7e"  # 0 と -8
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", DeprecationWarning)
            import audioop  # Python 3.12 まで
    except ImportError:
        return
    # audioop は負数を 2 bit 右シフトで丸めるので、4 の倍数だけで突き合わせる
    vals = list(range(-32768, 32768, 4))
    src = struct.pack("<%dh" % len(vals), *vals)
    dst = bytearray(len(vals))
    voice._ulaw(src, dst, len(vals))
    assert bytes(dst) == audioop.lin2ulaw(src, 2)


import os  # noqa: E402


def spool(voice):
    return os.path.exists(voice.SPOOL)


def test_start_begins_mic_and_queues_two_without_touching_speaker(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    # Speaker はアプリの起動時に止める。録音後に begin すると IDF ヒープを約 7.7KB 取り直すため
    assert log == ["mic.begin"] and len(mic.q) == 2 and v.state == "rec" and spool(voice)


def test_frames_are_sent_in_order_with_seq(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    for i in range(4):
        mic.finish(value=100 * (i + 1))
        v.service(link)
        assert len(mic.q) == 2  # 取り出したら次を積み直す
    v.service(link)  # 1 回の service で送るのは 1 フレーム（送信は main loop の pump に任せる）
    got = audio(link)
    # flash には 2 フレームずつ書く（1 フレームずつだと 1 回最大 55ms、2 つで 25ms：実機）ので、送れるのも 2 つずつ
    assert [s for s, _ in got] == [0, 1, 2, 3]
    assert got[1][1] == bytes([ulaw_ref(200)]) * 1600
    assert v.level == 400


def test_slow_link_keeps_every_frame_in_flash(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    link.idle = False  # 送信が追いつかない
    for _ in range(30):
        mic.finish()
        v.service(link)
    assert link.lines == [] and v._written == 30 and spool(voice)
    link.idle = True
    for _ in range(40):
        v.service(link)
    assert [s for s, _ in audio(link)] == list(range(30))  # 捨てずに、先頭から順に送る


def test_stop_sends_everything_then_voice_end_and_deletes_spool(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    link.idle = False
    for _ in range(5):
        mic.finish()
        v.service(link)
    v.stop()
    assert v.state == "flush"
    mic.finish()
    mic.finish()
    v.service(link)
    assert len(mic.q) == 0 and log[-1] == "mic.end"
    assert v.progress == 0 and d.replies() == []
    link.idle = True
    for _ in range(3):
        v.service(link)
    assert 0 < v.progress < 100 and v.state == "flush"
    for _ in range(10):
        v.service(link)
    assert [s for s, _ in audio(link)] == list(range(7))
    assert d.replies() == [{"t": "voice_end", "vid": vid}] and v.state == "wait"
    assert not spool(voice)


def test_cancel_stops_mic_sends_voice_cancel_and_deletes_spool(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    mic.finish()
    v.service(link)
    mic.q.clear()  # 実機では end の前に、待ちのバッファが終わるのを待つ
    v.cancel()
    assert d.replies() == [{"t": "voice_cancel", "vid": vid}]
    assert v.state == "idle" and log[-1:] == ["mic.end"] and not spool(voice)
    link.lines.clear()
    v.service(link)
    assert link.lines == []


def test_stops_at_60_seconds(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    for _ in range(600):
        mic.finish()
        v.service(link)
    for _ in range(5):
        v.service(link)
    assert v.elapsed_ms == 60000 and len(mic.q) == 0
    assert len(audio(link)) == 600
    assert v.state == "wait" and d.replies() == [{"t": "voice_end", "vid": v.vid}]


def test_session_change_aborts_without_cancel_and_deletes_spool(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    mic.q.clear()
    d.handshake(nh=bytes(16))
    v.service(link)
    assert v.state == "lost" and "切断" in v.err and d.sent == [] and "mic.end" in log
    assert not spool(voice)


def test_leftover_spool_is_deleted_at_startup(env):
    voice, d, v, mic, log, link = env
    with open(voice.SPOOL, "wb") as f:
        f.write(b"old")
    voice.Voice(d.p)
    assert not spool(voice)


def test_buffers_are_allocated_once_at_startup(env):
    voice, d, v, mic, log, link = env
    bufs = v._mic, v._wbuf, v._rbuf, v._tx
    start(d, v, link)
    mic.q.clear()
    v.cancel()
    d.replies()
    start(d, v, link)
    assert (v._mic, v._wbuf, v._rbuf, v._tx) == bufs


def test_memory_error_at_startup_disables_voice(env, monkeypatch):
    voice, d, v, mic, log, link = env

    def no_memory(n):
        raise MemoryError

    monkeypatch.setattr(voice, "bytearray", no_memory, raising=False)
    v2 = voice.Voice(d.p)
    assert v2.start("ja-JP") is False
    assert v2.state == "idle" and "メモリ" in v2.err and d.sent == [] and log == []


def test_flash_full_does_not_start(env, monkeypatch):
    voice, d, v, mic, log, link = env
    monkeypatch.setattr(voice, "_flash_free", lambda path: voice.MAX_FRAMES * voice.FRAME - 1)
    assert v.start("ja-JP") is False
    assert "flash" in v.err and d.sent == [] and log == [] and not spool(voice)


def test_flush_deadline_scales_with_recording_length(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    link.idle = False  # 送信が止まったまま
    for _ in range(50):  # 5 秒
        mic.finish()
        v.service(link)
    v.stop()
    mic.finish()
    mic.finish()
    v.service(link)
    voice.clock[0] += 5200 * 3 + 10000 - 1  # 録音 5.2 秒 × 3 + 10 秒まで待つ
    v.service(link)
    assert v.state == "flush"
    voice.clock[0] += 2
    v.service(link)
    assert v.state == "lost" and "送り切れ" in v.err and not spool(voice)
    assert d.replies() == [{"t": "voice_cancel", "vid": vid}]


def test_wait_times_out(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    v.stop()
    mic.finish()
    mic.finish()
    v.service(link)
    v.service(link)
    assert v.state == "wait"
    d.replies()
    voice.clock[0] += voice.WAIT_MS + 1
    v.service(link)
    assert v.state == "lost" and v.err
    assert d.replies() == [{"t": "voice_cancel", "vid": vid}]


def test_no_single_buffer_is_large(env, monkeypatch):
    voice, d, v, mic, log, link = env
    sizes = []

    def track(n):
        sizes.append(n)
        return bytearray(n)

    monkeypatch.setattr(voice, "bytearray", track, raising=False)
    import crypto

    monkeypatch.setattr(crypto, "bytearray", track, raising=False)
    voice.Voice(d.p)
    # 大きな連続領域を要求すると、GC ヒープは IDF ヒープの最大ブロックを丸ごと取って伸びる（実機で 31.7KB）
    assert sizes and max(sizes) <= 2 * voice.SAMPLES + 1024


def test_buffers_can_be_allocated_before_the_voice_object(env):
    voice, d, v, mic, log, link = env
    bufs = voice.alloc()
    v2 = voice.Voice(d.p, bufs=bufs)
    assert (v2._mic, v2._wbuf, v2._rbuf, v2._tx) == bufs


def test_wifi_link_waits_until_recording_stops(env):
    voice, d, v, mic, log, link = env
    link.kind = "Wi-Fi"  # Wi-Fi はマイクの動作中に送ると 68KB/s → 2〜12KB/s に落ちる（実機）
    start(d, v, link)
    for _ in range(6):
        mic.finish()
        v.service(link)
    assert link.lines == []
    v.stop()
    mic.finish()
    mic.finish()
    for _ in range(12):
        v.service(link)
    assert [s for s, _ in audio(link)] == list(range(8))
    assert d.replies() == [{"t": "voice_end", "vid": v.vid}]
