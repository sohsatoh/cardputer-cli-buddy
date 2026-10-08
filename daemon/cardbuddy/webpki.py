"""Web UI の mTLS 用 PKI（ローカル CA、サーバー証明書、端末のクライアント証明書と許可リスト）。

ファイルは CARDBUDDY_HOME/pki に置く：ca.key / ca.crt / server.key / server.crt / allowed.json（端末の指紋の許可リスト）。
"""

import datetime as dt
import hashlib
import ipaddress
import json
import os
import plistlib
import re
import secrets
import socket
import subprocess
import uuid
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

# iOS は信頼済みの TLS サーバー証明書に 398 日以内を要求する
SERVER_DAYS = CLIENT_DAYS = 397
RENEW_WITHIN_DAYS = 30
NAME = re.compile(r"[A-Za-z0-9_-]{1,32}")
PERMITTED = [x509.DNSName("local"), x509.DNSName("localhost"),
             *(x509.IPAddress(ipaddress.ip_network(n)) for n in
               ("127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"))]


def pki_dir() -> Path:
    from .buddyd import home
    return home() / "pki"


def lan_ip() -> str | None:
    # UDP の connect はパケットを送らずに既定経路のインターフェースを選ぶ
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))
            return s.getsockname()[0]
        except OSError:
            return None


def local_hostname() -> str:
    try:
        return subprocess.run(["scutil", "--get", "LocalHostName"], capture_output=True, text=True,
                              check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return socket.gethostname().split(".")[0]


def fingerprint(cert: x509.Certificate) -> str:
    return hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def _write(path: Path, data: bytes):
    # buddyd が allowed.json を読んでいる最中に書き換えても、途中の内容を読ませない
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


def _key():
    return ec.generate_private_key(ec.SECP256R1())


def _pem_key(k) -> bytes:
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                           serialization.NoEncryption())


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


KEYCHAIN_SERVICE = "cardbuddy-ca"


def keychain_get(account: str) -> str | None:
    r = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", account, "-w"],
                       capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def keychain_set(account: str, password: str):
    if '"' in account:
        raise SystemExit(f"buddy: cannot store a Keychain item for {account!r}")
    # パスフレーズを引数に載せると ps から見えるので、security の対話モードに標準入力で渡す
    cmd = f'add-generic-password -U -s {KEYCHAIN_SERVICE} -a "{account}" -w "{password}"\n'
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0 or keychain_get(account) != password:
        raise SystemExit(f"buddy: could not store the CA passphrase in the Keychain: {r.stderr.strip()}")


def _write_ca_key(key):
    """CA 鍵を、Keychain に置いたランダムなパスフレーズで暗号化して書く。"""
    password = secrets.token_urlsafe(32)
    keychain_set(str(pki_dir()), password)
    _write(pki_dir() / "ca.key", key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                    serialization.BestAvailableEncryption(password.encode())))


def _ca_key_encrypted() -> bool:
    return b"ENCRYPTED PRIVATE KEY" in (pki_dir() / "ca.key").read_bytes()


def _load_ca():
    d = pki_dir()
    if not (d / "ca.key").exists():
        raise SystemExit(f"buddy: no CA in {d}; run `buddy web init` first")
    password = None
    if _ca_key_encrypted():
        password = keychain_get(str(d))
        if password is None:
            raise SystemExit(f"buddy: the CA key passphrase for {d} is not in the Keychain (service {KEYCHAIN_SERVICE})")
        password = password.encode()
    return (serialization.load_pem_private_key((d / "ca.key").read_bytes(), password),
            x509.load_pem_x509_certificate((d / "ca.crt").read_bytes()))


def protect_ca() -> bool:
    """平文の CA 鍵を、同じ鍵のまま暗号化する（iPhone に入れた CA は変わらない）。暗号化したら True。"""
    if _ca_key_encrypted():
        return False
    key, _ = _load_ca()
    _write_ca_key(key)
    return True


def _issue(ca_key, ca_cert, cn: str, days: int, eku, san=None):
    now = dt.datetime.now(dt.timezone.utc)
    key = _key()
    b = (x509.CertificateBuilder()
         .subject_name(_name(cn))
         .issuer_name(ca_cert.subject)
         .public_key(key.public_key())
         .serial_number(x509.random_serial_number())
         # 端末の時計ずれで「まだ有効でない」にならないよう 1 日戻す
         .not_valid_before(now - dt.timedelta(days=1))
         .not_valid_after(now + dt.timedelta(days=days - 1))
         .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
         .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False), critical=True)
         .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
         .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
         .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False))
    if san:
        b = b.add_extension(x509.SubjectAlternativeName(san), critical=False)
    return key, b.sign(ca_key, hashes.SHA256())


def init(renew: bool = False) -> dict:
    d = pki_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(d, 0o700)
    if renew:
        ca_key, ca_cert = _load_ca()
    else:
        if (d / "ca.key").exists():
            raise SystemExit(f"buddy: {d} is already initialized; use `buddy web init --renew` for a new server cert")
        now = dt.datetime.now(dt.timezone.utc)
        ca_key = _key()
        ca_cert = (x509.CertificateBuilder()
                   .subject_name(_name("CardBuddy Local CA"))
                   .issuer_name(_name("CardBuddy Local CA"))
                   .public_key(ca_key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(now - dt.timedelta(days=1))
                   .not_valid_after(now + dt.timedelta(days=3650))
                   .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
                   .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False),
                                  critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
                   # iPhone で完全に信頼する CA なので、鍵が漏れても LAN 内の名前にしか証明書を出せないようにする
                   .add_extension(x509.NameConstraints(permitted_subtrees=PERMITTED, excluded_subtrees=None),
                                  critical=True)
                   .sign(ca_key, hashes.SHA256()))
        _write_ca_key(ca_key)
        _write(d / "ca.crt", ca_cert.public_bytes(serialization.Encoding.PEM))
    host, ip = local_hostname(), lan_ip()
    san = [x509.DNSName(f"{host}.local"), x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
    if ip:
        san.append(x509.IPAddress(ipaddress.ip_address(ip)))
    key, cert = _issue(ca_key, ca_cert, f"{host}.local", SERVER_DAYS, ExtendedKeyUsageOID.SERVER_AUTH, san)
    _write(d / "server.key", _pem_key(key))
    _write(d / "server.crt", cert.public_bytes(serialization.Encoding.PEM))
    if not (d / "allowed.json").exists():
        _write(d / "allowed.json", b"{}")
    return {"host": f"{host}.local", "ip": ip, "expires": cert.not_valid_after_utc}


def server_status(now: dt.datetime | None = None) -> dict:
    now = now or dt.datetime.now(dt.timezone.utc)
    cert = x509.load_pem_x509_certificate((pki_dir() / "server.crt").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    dns = san.get_values_for_type(x509.DNSName)
    ips = [str(a) for a in san.get_values_for_type(x509.IPAddress)]
    days = (cert.not_valid_after_utc - now).days
    warnings = []
    if (pki_dir() / "ca.key").exists() and not _ca_key_encrypted():
        warnings.append("the CA key is stored in plaintext; run `buddy web protect-ca`")
    if days < RENEW_WITHIN_DAYS:
        warnings.append(f"server certificate expires in {days} days; run `buddy web init --renew`")
    ip, host = lan_ip(), f"{local_hostname()}.local"
    if ip and ip not in ips:
        warnings.append(f"LAN IP {ip} is not in the server certificate; run `buddy web init --renew`")
    if host not in dns:
        warnings.append(f"{host} is not in the server certificate; run `buddy web init --renew`")
    return {"expires": cert.not_valid_after_utc, "days_left": days, "dns": dns, "ips": ips, "warnings": warnings}


def server_names() -> set[str]:
    cert = x509.load_pem_x509_certificate((pki_dir() / "server.crt").read_bytes())
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    # DNS 名は大文字小文字を区別しない（照合側は小文字にしたホスト名と比べる）
    return {*(d.lower() for d in san.get_values_for_type(x509.DNSName)),
            *map(str, san.get_values_for_type(x509.IPAddress))}


def load_allowed() -> dict[str, dict]:
    path = pki_dir() / "allowed.json"
    raw = json.loads(path.read_text()) if path.exists() else {}
    # スパイクで作った許可リストは値が名前の文字列なので、それも読む
    return {fp: v if isinstance(v, dict) else {"name": v, "expires": None} for fp, v in raw.items()}


def _save_allowed(allowed: dict):
    _write(pki_dir() / "allowed.json", json.dumps(allowed, indent=2, ensure_ascii=False).encode())


def _new_client(name: str):
    if not NAME.fullmatch(name):
        raise SystemExit(f"buddy: bad device name {name!r} (use letters, digits, '_' or '-', up to 32)")
    ca_key, ca_cert = _load_ca()
    key, cert = _issue(ca_key, ca_cert, name, CLIENT_DAYS, ExtendedKeyUsageOID.CLIENT_AUTH)
    allowed = load_allowed()
    fp = fingerprint(cert)
    allowed[fp] = {"name": name, "expires": cert.not_valid_after_utc.date().isoformat()}
    _save_allowed(allowed)
    return key, cert, ca_cert, fp


def _p12(key, cert, ca_cert, name: str, password: bytes) -> bytes:
    # iOS の一部の版は PBES2/AES の PKCS#12 を読めず「パスワードが違う」と出るので、旧来の 3DES + SHA1 MAC にする
    enc = (serialization.PrivateFormat.PKCS12.encryption_builder()
           .kdf_rounds(50000)
           .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC)
           .hmac_hash(hashes.SHA1())
           .build(password))
    return pkcs12.serialize_key_and_certificates(name.encode(), key, cert, [ca_cert], enc)


def _mobileconfig(ca_cert, p12: bytes, name: str) -> bytes:
    def payload(typ, ident, display, **kw):
        return {"PayloadType": typ, "PayloadVersion": 1, "PayloadIdentifier": f"local.cardbuddy.web.{ident}",
                "PayloadUUID": str(uuid.uuid4()).upper(), "PayloadDisplayName": display, **kw}

    # Password キーは入れない（入れるとプロファイルにパスワードが残り、iOS もインストール時に聞いてこなくなる）
    return plistlib.dumps(payload(
        "Configuration", name, f"CardBuddy Web ({name})",
        PayloadContent=[
            payload("com.apple.security.root", "ca", "CardBuddy Local CA",
                    PayloadContent=ca_cert.public_bytes(serialization.Encoding.DER),
                    PayloadCertificateFileName="cardbuddy-ca.cer"),
            payload("com.apple.security.pkcs12", f"identity.{name}", f"CardBuddy client ({name})",
                    PayloadContent=p12, PayloadCertificateFileName=f"cardbuddy-{name}.p12"),
        ],
        PayloadRemovalDisallowed=False), fmt=plistlib.FMT_XML)


def enroll(name: str, out_dir: Path) -> tuple[Path, str, str]:
    """iPhone 用の .mobileconfig を書き、(パス, 指紋, PKCS#12 のパスワード) を返す。パスワードは保存しない。"""
    key, cert, ca_cert, fp = _new_client(name)
    password = secrets.token_urlsafe(9)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"cardbuddy-{name}.mobileconfig"
    _write(path, _mobileconfig(ca_cert, _p12(key, cert, ca_cert, name, password.encode()), name))
    return path, fp, password


def enroll_pem(name: str) -> tuple[Path, str]:
    """curl などで使うクライアント証明書を PEM で pki/ に書き、(証明書のパス, 指紋) を返す。"""
    key, cert, _, fp = _new_client(name)
    crt = pki_dir() / f"client-{name}.crt"
    _write(crt.with_suffix(".key"), _pem_key(key))
    _write(crt, cert.public_bytes(serialization.Encoding.PEM))
    return crt, fp


def devices() -> list[dict]:
    return [{"fp": fp, **v} for fp, v in load_allowed().items()]


def revoke(name: str) -> int:
    allowed = load_allowed()
    keep = {fp: v for fp, v in allowed.items() if v["name"] != name}
    _save_allowed(keep)
    return len(allowed) - len(keep)
