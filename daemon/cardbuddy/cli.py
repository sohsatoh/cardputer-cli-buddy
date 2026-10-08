"""buddy コマンド: pair / status / install-agent / web。"""

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


def _to_terminal(line: str):
    # PKCS#12 のパスワードは、標準出力がファイルやパイプにつながっていても端末にだけ出す
    try:
        with open("/dev/tty", "w") as tty:
            tty.write(line + "\n")
    except OSError:
        raise SystemExit("buddy: the profile password can only be shown on a terminal; run this in Terminal") from None


def _web_urls() -> list[str]:
    from . import webpki
    from .web import WEB_PORT
    st = webpki.server_status()
    return [f"https://{h}:{WEB_PORT}/" for h in [*[d for d in st["dns"] if d != "localhost"],
                                                 *[i for i in st["ips"] if i != "127.0.0.1"]]]


def web(args) -> int:
    from . import webpki
    if args.web_cmd == "init":
        info = webpki.init(renew=args.renew)
        print(f"server certificate for {info['host']} / {info['ip']} (expires {info['expires']:%Y-%m-%d}) "
              f"written to {webpki.pki_dir()}")
        print(f"restart buddyd to use it (launchctl kickstart -k gui/{os.getuid()}/{LABEL})")
    elif args.web_cmd == "enroll" and args.pem:
        crt, fp = webpki.enroll_pem(args.name)
        key, ca = crt.with_suffix(".key"), webpki.pki_dir() / "ca.crt"
        print(f"{args.name}: sha256={fp[:16]}…\n  cert: {crt}\n  key:  {key}")
        print(f"  curl --cacert {ca} --cert {crt} --key {key} {_web_urls()[0]}")
    elif args.web_cmd == "enroll":
        path, fp, password = webpki.enroll(args.name, Path(args.out).expanduser())
        print(f"profile: {path}\nsha256:  {fp[:16]}…")
        _to_terminal(f"password (shown only here, not saved): {password}")
        print("Send the profile to the iPhone (AirDrop), install it in Settings, and enter the password.\n"
              "Then turn on full trust for \"CardBuddy Local CA\" in Settings > General > About > "
              "Certificate Trust Settings, and open:")
        for url in _web_urls():
            print(f"  {url}")
    elif args.web_cmd == "devices":
        for d in webpki.devices():
            print(f"  {d['name']:<16} sha256={d['fp'][:16]}…  expires {d['expires'] or '-'}")
        st = webpki.server_status()
        print(f"server certificate: expires {st['expires']:%Y-%m-%d} ({st['days_left']} days), "
              f"names {', '.join(st['dns'] + st['ips'])}")
        for w in st["warnings"]:
            print(f"  warning: {w}")
        for url in _web_urls():
            print(f"  {url}")
    elif args.web_cmd == "protect-ca":
        done = webpki.protect_ca()
        print(f"encrypted the CA key in {webpki.pki_dir()} with a passphrase stored in the Keychain"
              if done else "the CA key is already encrypted")
    else:
        n = webpki.revoke(args.name)
        print(f"revoked {n} certificate(s) for {args.name}" if n else f"no device named {args.name}")
        return 0 if n else 1
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
    w = sub.add_parser("web", help="manage the mTLS certificates of the LAN web UI")
    wsub = w.add_subparsers(dest="web_cmd", required=True)
    wi = wsub.add_parser("init", help="create the local CA and the server certificate")
    wi.add_argument("--renew", action="store_true", help="only reissue the server certificate (expiry or IP change)")
    we = wsub.add_parser("enroll", help="issue a client certificate for a device")
    we.add_argument("name")
    we.add_argument("--out", default="~/Downloads", help="where to write the .mobileconfig (default: %(default)s)")
    we.add_argument("--pem", action="store_true", help="write a PEM certificate and key for curl instead")
    wsub.add_parser("devices", help="list enrolled devices and the server certificate status")
    wsub.add_parser("protect-ca", help="encrypt a plaintext CA key with a passphrase kept in the Keychain")
    wr = wsub.add_parser("revoke", help="revoke every certificate of a device")
    wr.add_argument("name")
    args = ap.parse_args(argv)
    return {"pair": pair, "status": status, "install-agent": install_agent, "web": web}[args.cmd](args)
