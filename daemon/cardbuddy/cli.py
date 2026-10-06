"""buddy コマンド: pair / status / install-agent。"""

import argparse
import getpass
import http.client
import json
import os
import plistlib
import re
import socket
import sys
import time
from pathlib import Path

import serial

from .buddyd import home, load_key

LABEL = "com.sohsatoh.cardbuddy.buddyd"
DEVICE_KEY = "/flash/cardbuddy.key"
DEVICE_WIFI = "/flash/cardbuddy_wifi.json"
# raw REPL はエコーしないので、paste mode と違って鍵の hex がシリアルの出力に戻ってこない
DEVICE_CODE = """import binascii, os
k = binascii.unhexlify('{hex}')
f = open('{path}', 'wb')
f.write(k)
f.close()
del k
print(os.stat('{path}')[6])
"""


# device の buddy_ble._mac_suffix と同じ規則（Claude_ + MAC 下位 3 byte の大文字 hex）で広告名を作る
NAME_CODE = """import bluetooth
b = bluetooth.BLE()
a = b.active()
if not a:
    b.active(True)
print('Claude_' + ''.join('{:02X}'.format(x) for x in b.config('mac')[1][-3:]))
if not a:
    b.active(False)
"""


def _expect(s, token: bytes) -> bytes:
    data = s.read_until(token)
    if not data.endswith(token):
        raise SystemExit(f"buddy: no {token!r} from device (got {data[-80:]!r})")
    return data


def raw_exec(s, code: str) -> bytes:
    for _ in range(3):
        s.write(b"\r\x03")
        time.sleep(0.1)
    s.reset_input_buffer()
    s.write(b"\r\x01")
    _expect(s, b"raw REPL; CTRL-B to exit\r\n>")
    s.write(code.encode() + b"\x04")
    _expect(s, b"OK")
    out = _expect(s, b"\x04")[:-1]
    err = _expect(s, b"\x04>")[:-2]
    s.write(b"\x02")
    if err:
        raise SystemExit("buddy: device error:\n" + err.decode(errors="replace"))
    return out


def write_private(path: Path, data: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def pair(args) -> int:
    path = home() / "key"
    new = args.rotate or not path.exists()
    key = os.urandom(32) if new else load_key(path)
    wifi = None
    if args.wifi:
        ssid = input("Wi-Fi SSID: ").strip()
        psk = getpass.getpass("Wi-Fi password (not echoed): ")
        if not ssid:
            raise SystemExit("buddy: SSID is empty")
        wifi = json.dumps({"ssid": ssid, "psk": psk}).encode()
    with serial.Serial(args.port, 115200, timeout=5) as s:
        name = raw_exec(s, NAME_CODE).decode(errors="replace").strip()
        if not re.fullmatch(r"Claude_[0-9A-F]{6}", name):
            raise SystemExit(f"buddy: unexpected device name: {name!r}")
        out = raw_exec(s, DEVICE_CODE.format(hex=key.hex(), path=DEVICE_KEY))
        if out.strip() != b"32":
            raise SystemExit(f"buddy: {DEVICE_KEY} has unexpected size: {out.strip()!r}")
        if wifi is not None:
            out = raw_exec(s, DEVICE_CODE.format(hex=wifi.hex(), path=DEVICE_WIFI))
            if out.strip() != str(len(wifi)).encode():
                raise SystemExit(f"buddy: {DEVICE_WIFI} has unexpected size: {out.strip()!r}")
            print(f"Wi-Fi settings written to {DEVICE_WIFI}")
    # デバイスへの書き込みが成功してからホスト側を差し替え、片側だけ新しい鍵になるのを避ける
    if new:
        write_private(path, key)
    write_private(home() / "device", name.encode())
    print(f"paired with {name}: {DEVICE_KEY} written ({'new key' if new else 'existing key'}). "
          "Reset the device to load it.")
    if new:
        print("if buddyd is running, restart it to load the new key "
              f"(launchd: launchctl kickstart -k gui/{os.getuid()}/{LABEL})")
    return 0


class _UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=5)
        self.path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def status(args) -> int:
    sock = home() / "buddyd.sock"
    conn = _UnixHTTPConnection(str(sock))
    try:
        conn.request("GET", "/status")
        st = json.loads(conn.getresponse().read())
    except OSError as e:
        print(f"buddyd is not running ({sock}: {e})", file=sys.stderr)
        return 1
    finally:
        conn.close()
    state = f"connected over {st.get('transport')}" if st["connected"] else "not connected"
    print(f"device: {st['device'] or '-'} ({state})")
    for s in st["sessions"]:
        print(f"  #{s['n'] or '-'}  {s['state']:<8} {s['sid']}")
    return 0


def install_agent(args) -> int:
    exe = Path(sys.executable).parent / "buddyd"
    if not exe.exists():
        raise SystemExit(f"buddy: {exe} not found; install the package into this environment first")
    h = home()
    h.mkdir(mode=0o700, parents=True, exist_ok=True)
    plist = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [str(exe)],
        "EnvironmentVariables": {"CARDBUDDY_HOME": str(h)},
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": str(h / "buddyd.log"),
        "StandardErrorPath": str(h / "buddyd.log"),
    }))
    print(f"wrote {plist}\nto start it, run:\n  launchctl bootstrap gui/{os.getuid()} {plist}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="buddy")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pair", help="create the shared key and write it to the device over USB")
    p.add_argument("--port", required=True, help="serial port of the Cardputer")
    p.add_argument("--rotate", action="store_true", help="replace the existing key")
    p.add_argument("--wifi", action="store_true", help="also write Wi-Fi SSID / password (prompted) to the device")
    sub.add_parser("status", help="show buddyd status")
    sub.add_parser("install-agent", help="write the launchd LaunchAgent plist")
    args = ap.parse_args(argv)
    return {"pair": pair, "status": status, "install-agent": install_agent}[args.cmd](args)
