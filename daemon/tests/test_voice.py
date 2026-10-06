import asyncio
import functools
import logging
import os
import stat
import tempfile
import types
import warnings
import wave
from pathlib import Path

import pytest

from cardbuddy import voice
from cardbuddy.buddyd import Hub
from cardbuddy.voice import CHUNK, MAX_BYTES, Recorder

SILENCE = b"\xff"


def aio(f):
    @functools.wraps(f)
    def run(*a, **k):
        return asyncio.run(f(*a, **k))
    return run


def frame(seq: int, data: bytes) -> bytes:
    return seq.to_bytes(2, "big") + data


def test_ulaw_decode_known_values():
    t = voice.ULAW
    assert (t[0xFF], t[0x7F], t[0x00], t[0x80], t[0xF0], t[0x70], t[0xEF]) == (0, 0, -32124, 32124, 120, -120, 132)


def test_ulaw_decode_matches_audioop_when_available():
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        audioop = pytest.importorskip("audioop")
    pcm = audioop.ulaw2lin(bytes(range(256)), 2)
    assert voice.ulaw_to_pcm16(bytes(range(256))) == pcm


def test_recorder_fills_gaps_and_drops_late_frames():
    r = Recorder()
    r.begin("v1", "ja-JP")
    r.audio(frame(0, b"\x01" * CHUNK))
    r.audio(frame(3, b"\x02" * 10))
    r.audio(frame(2, b"\x03" * CHUNK))
    r.audio(frame(3, b"\x04" * 10))
    r.audio(b"\x00")
    assert r.end("v1") == (b"\x01" * CHUNK + SILENCE * (2 * CHUNK) + b"\x02" * 10, "ja-JP")
    assert r.end("v1") is None


def test_recorder_caps_at_60_seconds():
    r = Recorder()
    r.begin("v1", "en-US")
    last = MAX_BYTES // CHUNK - 1
    r.audio(frame(0, b"\x01" * CHUNK))
    r.audio(frame(last, b"\x02" * CHUNK))
    r.audio(frame(last + 1, b"\x03" * CHUNK))
    r.audio(frame(60000, b"\x04" * CHUNK))
    data, _ = r.end("v1")
    assert len(data) == MAX_BYTES and data.endswith(b"\x02" * CHUNK)
    r.begin("v2", "en-US")
    r.audio(frame(0, b"\x05" * (CHUNK + 1)))
    assert r.end("v2") == (b"", "en-US")


def test_recorder_ignores_audio_and_mismatched_vids_when_not_recording():
    r = Recorder()
    r.audio(frame(0, b"\x01"))
    assert r.end("v1") is None
    r.begin("v1", "ja-JP")
    r.audio(frame(0, b"\x01"))
    assert r.end("other") is None
    r.cancel("other")
    assert r.end("v1") == (b"\x01", "ja-JP")
    r.begin("v2", "ja-JP")
    r.audio(frame(0, b"\x01"))
    r.cancel("v2")
    r.audio(frame(1, b"\x01"))
    assert r.end("v2") is None
    r.begin("v3", "ja-JP")
    r.audio(frame(0, b"\x01"))
    r.begin("v4", "ja-JP")
    assert r.end("v3") is None
    assert r.end("v4") == (b"", "ja-JP")
    r.begin("v5", "ja-JP")
    r.reset()
    assert r.end("v5") is None


def test_write_wav_header_content_and_permissions():
    d = Path(tempfile.mkdtemp(prefix="cb")) / "tmp"
    path = voice.write_wav(d, bytes([0x00, 0x80, 0xFF, 0xF0]))
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(d.stat().st_mode) == 0o700
        with wave.open(str(path)) as w:
            assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 16000, 4)
            frames = w.readframes(4)
        assert [int.from_bytes(frames[i:i + 2], "little", signed=True) for i in range(0, 8, 2)] == [
            -32124, 32124, 0, 120]
    finally:
        path.unlink()


def test_write_wav_removes_file_on_failure(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cb"))

    def boom(_):
        raise RuntimeError("decode failed")

    monkeypatch.setattr(voice, "ulaw_to_pcm16", boom)
    with pytest.raises(RuntimeError):
        voice.write_wav(d, b"\xff")
    assert list(d.iterdir()) == []


class FakeLink:
    connected = True
    transport = "ble"
    device = "Claude_ab12cd"

    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)


class SttError(Exception):
    pass


def fake_stt(result=None, exc=None, gate=None, seen=None):
    async def transcribe(wav, lang):
        assert isinstance(wav, str)
        with wave.open(wav) as w:
            seen.append((lang, w.getnframes(), stat.S_IMODE(os.stat(wav).st_mode), wav))
        if gate:
            await gate.wait()
        if exc:
            raise exc
        return result
    return types.SimpleNamespace(transcribe=transcribe, SttError=SttError)


@pytest.fixture
def home(monkeypatch):
    d = tempfile.mkdtemp(prefix="cb")
    monkeypatch.setenv("CARDBUDDY_HOME", d)
    return Path(d)


async def record(hub, vid, n=3, lang="ja-JP"):
    hub.on_msg({"t": "voice_begin", "vid": vid, "lang": lang})
    for i in range(n):
        hub.on_audio(frame(i, b"\x00" * CHUNK))


async def finish(hub):
    if hub.voice_task:
        await asyncio.gather(hub.voice_task, return_exceptions=True)


@aio
async def test_voice_end_transcribes_and_deletes_wav(home, caplog):
    caplog.set_level(logging.DEBUG)
    seen = []
    hub = Hub(FakeLink(), stt=fake_stt(result="ひみつの文字起こし", seen=seen))
    await record(hub, "v1")
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await finish(hub)
    assert hub.link.sent == [{"t": "voice_text", "vid": "v1", "text": "ひみつの文字起こし"}]
    (lang, frames, mode, wav), = seen
    assert (lang, frames, mode) == ("ja-JP", 3 * CHUNK, 0o600)
    assert not os.path.exists(wav) and list((home / "tmp").iterdir()) == []
    assert stat.S_IMODE((home / "tmp").stat().st_mode) == 0o700
    assert "ひみつ" not in caplog.text and "0.3s" in caplog.text


@aio
async def test_voice_text_is_capped_at_prompt_length(home):
    hub = Hub(FakeLink(), stt=fake_stt(result="あ" * 600, seen=[]))
    await record(hub, "v1")
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await finish(hub)
    assert hub.link.sent[0]["text"] == "あ" * 499 + "…"


@pytest.mark.parametrize("exc,err", [(SttError("認識できませんでした"), "認識できませんでした"),
                                     (RuntimeError("secret detail"), "transcription failed")])
@aio
async def test_voice_errors_delete_wav_and_report(home, caplog, exc, err):
    seen = []
    hub = Hub(FakeLink(), stt=fake_stt(exc=exc, seen=seen))
    await record(hub, "v1")
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await finish(hub)
    assert hub.link.sent == [{"t": "voice_error", "vid": "v1", "err": err}]
    assert not os.path.exists(seen[0][3])


@aio
async def test_empty_recording_is_an_error_without_stt(home):
    seen = []
    hub = Hub(FakeLink(), stt=fake_stt(result="x", seen=seen))
    hub.on_msg({"t": "voice_begin", "vid": "v1", "lang": "en-US"})
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await finish(hub)
    assert seen == [] and [m["t"] for m in hub.link.sent] == ["voice_error"]


@aio
async def test_superseded_transcription_is_not_sent(home):
    gate = asyncio.Event()
    hub = Hub(FakeLink(), stt=fake_stt(result="old", gate=gate, seen=[]))
    await record(hub, "v1")
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await asyncio.sleep(0.05)
    hub.on_msg({"t": "voice_begin", "vid": "v2", "lang": "ja-JP"})
    gate.set()
    await finish(hub)
    assert hub.link.sent == []


@aio
async def test_disconnect_and_rehello_cancel_recording(home):
    gate = asyncio.Event()
    hub = Hub(FakeLink(), stt=fake_stt(result="x", gate=gate, seen=[]))
    await record(hub, "v1")
    hub.on_down()
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    await record(hub, "v2")
    hub.on_msg({"t": "voice_end", "vid": "v2"})
    await asyncio.sleep(0.05)
    hub.on_up()
    gate.set()
    await finish(hub)
    assert [m["t"] for m in hub.link.sent] == ["sessions"]
    await record(hub, "v3")
    hub.on_up()
    hub.on_msg({"t": "voice_end", "vid": "v3"})
    await finish(hub)
    assert [m["t"] for m in hub.link.sent] == ["sessions", "sessions"]


@aio
async def test_voice_cancel_and_bad_messages(home, caplog):
    seen = []
    hub = Hub(FakeLink(), stt=fake_stt(result="x", seen=seen))
    await record(hub, "v1")
    hub.on_msg({"t": "voice_cancel", "vid": "other"})
    hub.on_msg({"t": "voice_cancel", "vid": "v1"})
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    for bad in ({"t": "voice_begin", "vid": "toolongvid", "lang": "ja-JP"},
                {"t": "voice_begin", "vid": "v-2", "lang": "ja-JP"},
                {"t": "voice_begin", "vid": "v2", "lang": "fr-FR"},
                {"t": "voice_end", "vid": 3}):
        hub.on_msg(bad)
    hub.on_audio(frame(0, b"\x00"))
    hub.on_msg({"t": "voice_end", "vid": "v2"})
    await finish(hub)
    assert seen == [] and hub.link.sent == []
    assert "drop voice" in caplog.text


def tracking_stt(state):
    async def transcribe(wav, lang):
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        state["wavs"].append(wav)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            state["cancelled"] += 1
            raise
        finally:
            state["active"] -= 1
    return types.SimpleNamespace(transcribe=transcribe, SttError=SttError)


@aio
async def test_at_most_one_transcription_and_cancel_paths(home):
    st = {"active": 0, "max": 0, "cancelled": 0, "wavs": []}
    hub = Hub(FakeLink(), stt=tracking_stt(st))

    async def start(vid):
        await record(hub, vid)
        hub.on_msg({"t": "voice_end", "vid": vid})
        await asyncio.sleep(0.05)
        return hub.voice_task

    t1 = await start("v1")
    t2 = await start("v2")
    assert t1.cancelled() and st["active"] == 1
    hub.on_msg({"t": "voice_cancel", "vid": "other"})
    await asyncio.sleep(0.05)
    assert not t2.done()
    hub.on_msg({"t": "voice_cancel", "vid": "v2"})
    await asyncio.sleep(0.05)
    assert t2.cancelled()
    t3 = await start("v3")
    hub.on_down()
    await asyncio.sleep(0.05)
    t4 = await start("v4")
    hub.on_up()
    await asyncio.sleep(0.05)
    assert t3.cancelled() and t4.cancelled()
    assert st == {"active": 0, "max": 1, "cancelled": 4, "wavs": st["wavs"]}
    assert all(not os.path.exists(w) for w in st["wavs"]) and list((home / "tmp").iterdir()) == []
    assert [m["t"] for m in hub.link.sent] == ["sessions"]


@aio
async def test_cancel_kills_real_stt_child_and_removes_wav(home, monkeypatch):
    pidfile = home / "pid"
    fake = home / "fake-stt"
    fake.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec sleep 30\n')
    fake.chmod(0o700)
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(fake))
    hub = Hub(FakeLink())
    await record(hub, "v1")
    hub.on_msg({"t": "voice_end", "vid": "v1"})
    for _ in range(100):
        if pidfile.exists() and pidfile.read_text().strip():
            break
        await asyncio.sleep(0.05)
    pid = int(pidfile.read_text())
    os.kill(pid, 0)
    task = hub.voice_task
    hub.on_msg({"t": "voice_begin", "vid": "v2", "lang": "ja-JP"})
    await asyncio.gather(task, return_exceptions=True)
    assert task.cancelled()
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert list((home / "tmp").iterdir()) == [] and hub.link.sent == []
