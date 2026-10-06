"""オンデバイス文字起こしヘルパー（daemon/stt/stt、macOS 26 の SpeechTranscriber）を呼ぶ薄いラッパー。"""

import asyncio
import json
import os
from pathlib import Path

BIN = Path(__file__).resolve().parent.parent / "stt" / "stt"
LANGS = ("ja-JP", "en-US")
# 初回はモデルのダウンロードが入るので、60 秒の音声の認識より長めに待つ
TIMEOUT = 180.0


class SttError(Exception):
    pass


async def transcribe(wav: Path, lang: str, *, timeout: float = TIMEOUT) -> str:
    if lang not in LANGS:
        raise SttError(f"unsupported lang: {lang}")
    exe = os.environ.get("CARDBUDDY_STT_BIN") or str(BIN)
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, "--lang", lang, str(wav),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        raise SttError(f"cannot run {exe} ({e.strerror}); build it with `make -C daemon/stt`") from None
    # communicate() はタイムアウトで途中まで読んだ stderr を捨てるので、自前で読む
    out_t = asyncio.create_task(proc.stdout.read())
    err_t = asyncio.create_task(proc.stderr.read())
    timed_out = False
    try:
        await asyncio.wait_for(proc.wait(), timeout)
    except asyncio.TimeoutError:
        timed_out = True
    finally:
        # 切断などでキャンセルされても子を残さない（残ると削除済みの音声を読み続け、タイムアウトも効かない）
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
    out, err = await out_t, (await err_t).decode(errors="replace").strip()
    if timed_out:
        last = err.splitlines()[-1] if err else "no output"
        raise SttError(f"stt timeout after {timeout:.0f}s ({last}); the first run downloads the speech model, "
                       f"try `daemon/stt/stt --lang {lang} --prepare`")
    try:
        res = json.loads(out.decode().strip().splitlines()[-1])
    except (IndexError, ValueError):
        raise SttError(f"stt exited {proc.returncode}: {err}") from None
    if not isinstance(res, dict):
        raise SttError(f"stt printed unexpected output (exit {proc.returncode})")
    if res.get("error") or not isinstance(res.get("text"), str):
        raise SttError(res.get("error") or "no text")
    return res["text"]
