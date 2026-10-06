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
        self.lines.append(line)


def ulaw_ref(v):
    sign = 0x80 if v < 0 else 0
    v = min(abs(v), 32635) + 0x84
    e = 7
    while e > 0 and not (v & (0x4000 >> (7 - e))):
        e -= 1
    return 0xFF ^ (sign | (e << 4) | ((v >> (e + 3)) & 0x0F))


@pytest.fixture
def env(monkeypatch):
    log = []
    m5 = types.SimpleNamespace(Mic=FakeMic(log), Speaker=FakeSpeaker(log))
    monkeypatch.setitem(sys.modules, "M5", m5)
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.delitem(sys.modules, "voice", raising=False)
    import voice

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


def test_start_switches_speaker_to_mic_and_queues_two(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    assert log == ["spk.end", "mic.begin"] and len(mic.q) == 2 and v.state == "rec"


def test_frames_are_sent_in_order_with_seq(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    for i in range(3):
        mic.finish(value=100 * (i + 1))
        v.service(link)
        assert len(mic.q) == 2  # 取り出したら次を積み直す
    got = audio(link)
    assert [s for s, _ in got] == [0, 1, 2]
    assert got[1][1] == bytes([ulaw_ref(200)]) * 1600
    assert v.level == 300


def test_ring_overflow_drops_oldest_and_keeps_seq(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    link.idle = False
    for _ in range(10):
        mic.finish()
        v.service(link)
    assert link.lines == [] and v.dropped == 10 - voice.SLOTS
    link.idle = True
    for _ in range(voice.SLOTS + 2):
        v.service(link)
    assert [s for s, _ in audio(link)] == list(range(10 - voice.SLOTS, 10))


def test_stop_flushes_then_sends_voice_end(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    mic.finish()
    link.idle = False
    v.service(link)
    v.stop()
    assert v.state == "flush"
    mic.finish()
    v.service(link)
    assert len(mic.q) == 1  # 止めたら積み直さない
    mic.finish()
    v.service(link)
    assert log[-2:] == ["mic.end", "spk.begin"] and d.replies() == []
    link.idle = True
    v.service(link)
    v.service(link)
    v.service(link)
    assert [s for s, _ in audio(link)] == [0, 1, 2]
    v.service(link)
    assert d.replies() == [{"t": "voice_end", "vid": vid}] and v.state == "wait"


def test_cancel_stops_mic_and_sends_voice_cancel(env):
    voice, d, v, mic, log, link = env
    vid = start(d, v, link)
    mic.finish()
    v.service(link)
    mic.q.clear()  # 実機では end の前に、待ちのバッファが終わるのを待つ
    v.cancel()
    assert d.replies() == [{"t": "voice_cancel", "vid": vid}]
    assert v.state == "idle" and log[-2:] == ["mic.end", "spk.begin"]
    link.lines.clear()
    v.service(link)
    assert link.lines == []


def test_stops_at_60_seconds(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    for _ in range(600):
        mic.finish()
        v.service(link)
    assert v.elapsed_ms == 60000 and len(mic.q) == 0
    assert len(audio(link)) == 600
    assert v.state == "wait" and d.replies() == [{"t": "voice_end", "vid": v.vid}]


def test_session_change_aborts_without_cancel(env):
    voice, d, v, mic, log, link = env
    start(d, v, link)
    mic.q.clear()
    d.handshake(nh=bytes(16))
    v.service(link)
    assert v.state == "lost" and d.sent == [] and "mic.end" in log
