import asyncio
import os
import shutil
import stat
import struct
import subprocess
from pathlib import Path

import pytest

from cardbuddy import stt


def fake_bin(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "stt"
    p.write_text("#!/bin/sh\n" + body + "\n")
    p.chmod(p.stat().st_mode | stat.S_IXUSR)
    return p


def run(*a, **kw):
    return asyncio.run(stt.transcribe(*a, **kw))


def test_returns_text_and_passes_args(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, 'printf \'{"text": "%s", "error": null}\\n\' "$*"')
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    assert run(Path("a.wav"), "en-US") == "--lang en-US a.wav"


def test_error_field_raises(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, 'echo \'{"text": null, "error": "speech model for ja-JP could not be downloaded"}\'; exit 1')
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    with pytest.raises(stt.SttError, match="could not be downloaded"):
        run(Path("a.wav"), "ja-JP")


def test_garbage_output_raises(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, "echo boom >&2; exit 134")
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    with pytest.raises(stt.SttError, match="134"):
        run(Path("a.wav"), "ja-JP")


def test_missing_binary_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(tmp_path / "nope"))
    with pytest.raises(stt.SttError, match="make -C daemon/stt"):
        run(Path("a.wav"), "ja-JP")


def test_timeout_kills(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, "exec sleep 30")
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    with pytest.raises(stt.SttError, match="timeout"):
        run(Path("a.wav"), "ja-JP", timeout=0.2)


def test_timeout_reports_last_stderr_line(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, "echo 'stt: downloading the on-device speech model for ja-JP' >&2; exec sleep 30")
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    with pytest.raises(stt.SttError, match="downloading"):
        run(Path("a.wav"), "ja-JP", timeout=0.5)


def test_cancel_kills_child(tmp_path, monkeypatch):
    pidfile = tmp_path / "pid"
    b = fake_bin(tmp_path, f"echo $$ > {pidfile}; exec sleep 30")
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))

    async def main():
        t = asyncio.create_task(stt.transcribe(Path("a.wav"), "ja-JP"))
        while not pidfile.exists() or not pidfile.read_text().strip():
            await asyncio.sleep(0.01)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t

    asyncio.run(main())
    with pytest.raises(ProcessLookupError):
        os.kill(int(pidfile.read_text()), 0)


def test_non_object_json_raises(tmp_path, monkeypatch):
    b = fake_bin(tmp_path, "echo null")
    monkeypatch.setenv("CARDBUDDY_STT_BIN", str(b))
    with pytest.raises(stt.SttError):
        run(Path("a.wav"), "ja-JP")


def test_rejects_unknown_lang():
    with pytest.raises(stt.SttError):
        run(Path("a.wav"), "fr-FR")


def ulaw_wav(tmp_path: Path, voice: str, text: str) -> Path:
    aiff, wav = tmp_path / "v.aiff", tmp_path / "v.wav"
    subprocess.run(["say", "-v", voice, "-o", str(aiff), text], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "ulaw@16000", "-c", "1", str(aiff), str(wav)], check=True)
    return wav


real = pytest.mark.skipif(not (stt.BIN.exists() and shutil.which("say") and shutil.which("afconvert")),
                          reason="needs `make -C daemon/stt`, say and afconvert")


@real
@pytest.mark.parametrize("lang,voice,text,expect", [
    ("ja-JP", "Kyoko", "今日は天気が良いので公園まで散歩に行きます", "公園"),
    ("en-US", "Samantha", "Please run the unit tests and fix the bug", "unit tests"),
])
def test_real_binary_ulaw16k(tmp_path, monkeypatch, lang, voice, text, expect):
    monkeypatch.delenv("CARDBUDDY_STT_BIN", raising=False)
    assert expect in run(ulaw_wav(tmp_path, voice, text), lang)


@real
def test_real_binary_empty_audio(tmp_path, monkeypatch):
    monkeypatch.delenv("CARDBUDDY_STT_BIN", raising=False)
    wav = tmp_path / "empty.wav"
    # WAVE_FORMAT_MULAW(7), mono, 16kHz, 8bit, data は 0 byte
    fmt = struct.pack("<HHIIHHH", 7, 1, 16000, 16000, 1, 8, 0)
    body = b"WAVE" + b"fmt " + struct.pack("<I", len(fmt)) + fmt + b"data" + struct.pack("<I", 0)
    wav.write_bytes(b"RIFF" + struct.pack("<I", len(body)) + body)
    assert run(wav, "ja-JP", timeout=20) == ""


@real
def test_real_binary_unreadable_audio(tmp_path, monkeypatch):
    monkeypatch.delenv("CARDBUDDY_STT_BIN", raising=False)
    bad = tmp_path / "bad.wav"
    bad.write_bytes(b"not a wav")
    with pytest.raises(stt.SttError, match="cannot read audio"):
        run(bad, "ja-JP")
