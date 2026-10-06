"""Cardputer CLI Buddy の device/ 一式を、UIFlow 2.0 の Cardputer-Adv に書き込む。

    uv --directory daemon run python ../device/scripts/deploy.py --port /dev/cu.usbmodemXXXX

--port を省くと .mpy へのコンパイルだけを行い、結果を表示して終わる。

- 空きメモリが約 60KB しかないため、大きいモジュールは mpy-cross で .mpy にして入れる。
  mpy-cross はデバイスの MicroPython に合わせる（UIFlow2 v2.4.2 は 1.25、mpy v6.3）。
- MicroPython は同名の .py を .mpy より先に import するので、デバイス上の .py は消す。
- main.py（ランチャー）を起動させるため、NVS の uiflow.boot_option を 2 にする。
"""

import argparse
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import push  # noqa: E402

DEVICE = Path(__file__).resolve().parent.parent
COMPILED = ["crypto.py", "buddy_protocol.py", "buddy_ble.py", "buddy_ui_cp.py", "kana.py", "voice.py"]
# viper を含むファイルは、アーキテクチャを指定しないと mpy-cross が "invalid arch" で失敗する
NATIVE = {"voice.py": "xtensawin"}  # ESP32-S3
PLAIN = [f for f in push.DEFAULT_FILES if f not in COMPILED]

RM_CODE = """import uos
for f in {names!r}:
    try:
        uos.remove('/flash/' + f)
    except OSError:
        pass
print('RM-OK')
"""

NVS_CODE = """import esp32
nvs = esp32.NVS("uiflow")
try: nvs.erase_key("boot_option")
except Exception: pass
nvs.set_u8("boot_option", 2)
nvs.commit()
print("NVS-OK")
"""


def compile_mpy(version: str, out: Path) -> list[Path]:
    cmd = ["uvx", "--from", f"mpy-cross=={version}.*", "mpy-cross"]
    subprocess.run(cmd + ["--version"], check=True)
    paths = []
    for name in COMPILED:
        dst = out / (name[:-3] + ".mpy")
        # .mpy にはソースのパスが埋め込まれるので、手元の絶対パスが入らないよう相対名で渡す
        arch = ["-march=" + NATIVE[name]] if name in NATIVE else []
        subprocess.run(cmd + arch + ["-o", str(dst), name], cwd=DEVICE, check=True)
        print(f"{dst.name}: {dst.stat().st_size} bytes")
        paths.append(dst)
    return paths


def run(s, code: str, ok: str) -> None:
    out = push._paste(s, code, settle=0.5)
    # paste mode は送ったコードを "=== " 付きでエコーするので、行頭に出た ok だけを実行結果とみなす
    if "Traceback" in out or "\n" + ok not in out:
        raise RuntimeError(f"device did not print {ok}:\n{out}")


def deploy(port: str, mpys: list[Path]) -> None:
    for name in PLAIN:
        if not (DEVICE / name).is_file():
            raise SystemExit(f"missing source: {DEVICE / name}")
    s = push.serial.Serial(port, 115200, timeout=1.0)
    try:
        # buddy pair などが raw REPL のまま残していても、通常の REPL に戻してから paste mode を使う
        s.write(b"\x03\x03")
        time.sleep(0.1)
        s.write(b"\x02")
        push._drain(s, wait=0.5)
        push._interrupt(s)

        for p in mpys:
            print(f"uploading {p.name}...")
            push._upload_file(s, str(p), p.name)
        for name in PLAIN:
            print(f"uploading {name}...")
            push._upload_file(s, str(DEVICE / name), name)

        run(s, RM_CODE.format(names=COMPILED), "RM-OK")
        run(s, NVS_CODE, "NVS-OK")
        print("rebooting device (re-plug USB if the port does not come back)...")
        push._paste(s, "import machine; machine.reset()\n", settle=0.5)
    finally:
        s.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Compile and deploy Cardputer CLI Buddy to a Cardputer-Adv.")
    ap.add_argument("--port", help="serial port of the Cardputer; omit to compile only")
    ap.add_argument("--mpy-cross", default="1.25", help="mpy-cross version matching the device MicroPython")
    args = ap.parse_args()
    with tempfile.TemporaryDirectory() as tmp:
        mpys = compile_mpy(args.mpy_cross, Path(tmp))
        if args.port:
            deploy(args.port, mpys)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
