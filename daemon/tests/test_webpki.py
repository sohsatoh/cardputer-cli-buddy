import datetime as dt
import ipaddress
import json
import plistlib
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.serialization import pkcs12

from cardbuddy import webpki


@pytest.fixture
def pki(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cb"))
    monkeypatch.setenv("CARDBUDDY_HOME", str(d / "h"))
    monkeypatch.setattr(webpki, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(webpki, "local_hostname", lambda: "macbook")
    webpki.init()
    return d / "h" / "pki"


def mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_init_creates_ca_and_server_cert(pki):
    assert mode(pki) == 0o700
    assert {p.name for p in pki.iterdir()} == {"ca.key", "ca.crt", "server.key", "server.crt", "allowed.json"}
    assert all(mode(p) == 0o600 for p in pki.iterdir())
    ca = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    srv = x509.load_pem_x509_certificate((pki / "server.crt").read_bytes())
    san = srv.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert set(san.get_values_for_type(x509.DNSName)) == {"macbook.local", "localhost"}
    assert set(san.get_values_for_type(x509.IPAddress)) == {
        ipaddress.ip_address("192.168.1.20"), ipaddress.ip_address("127.0.0.1")}
    assert srv.not_valid_after_utc - srv.not_valid_before_utc <= dt.timedelta(days=398)
    assert json.loads((pki / "allowed.json").read_text()) == {}
    with pytest.raises(SystemExit, match="--renew"):
        webpki.init()


def test_renew_keeps_ca_and_allowlist(pki, monkeypatch):
    ca, srv = (pki / "ca.crt").read_bytes(), (pki / "server.crt").read_bytes()
    _, fp = webpki.enroll_pem("mac")
    monkeypatch.setattr(webpki, "lan_ip", lambda: "10.0.0.5")
    webpki.init(renew=True)
    assert (pki / "ca.crt").read_bytes() == ca and (pki / "server.crt").read_bytes() != srv
    assert fp in webpki.load_allowed()
    assert webpki.server_status()["warnings"] == []


def test_enroll_writes_profile_and_allows_fingerprint(pki):
    out = pki.parent.parent / "dl"
    path, fp, password = webpki.enroll("iphone", out)
    assert path == out / "cardbuddy-iphone.mobileconfig" and mode(path) == 0o600
    raw = path.read_bytes()
    assert password.encode() not in raw
    plist = plistlib.loads(raw)
    root, ident = plist["PayloadContent"]
    assert (root["PayloadType"], ident["PayloadType"]) == ("com.apple.security.root", "com.apple.security.pkcs12")
    assert "Password" not in ident
    p12 = pkcs12.load_pkcs12(ident["PayloadContent"], password.encode())
    assert webpki.fingerprint(p12.cert.certificate) == fp
    assert webpki.load_allowed()[fp]["name"] == "iphone"
    with pytest.raises(SystemExit):
        webpki.enroll("bad name!", out)


def test_enroll_pem_and_revoke(pki):
    crt, fp = webpki.enroll_pem("mac")
    assert mode(crt) == 0o600 and mode(crt.with_suffix(".key")) == 0o600
    webpki.enroll_pem("other")
    assert [d["name"] for d in webpki.devices()] == ["mac", "other"]
    assert webpki.revoke("mac") == 1
    assert webpki.revoke("mac") == 0
    assert fp not in webpki.load_allowed() and [d["name"] for d in webpki.devices()] == ["other"]


def test_legacy_allowlist_format_is_accepted(pki):
    (pki / "allowed.json").write_text(json.dumps({"ab" * 32: "iphone"}))
    assert webpki.load_allowed() == {"ab" * 32: {"name": "iphone", "expires": None}}
    webpki.enroll_pem("mac")
    assert webpki.load_allowed()["ab" * 32]["name"] == "iphone"


def test_server_status_warns_on_ip_change_and_expiry(pki, monkeypatch):
    assert webpki.server_status()["warnings"] == []
    monkeypatch.setattr(webpki, "lan_ip", lambda: "10.0.0.9")
    assert any("10.0.0.9" in w and "--renew" in w for w in webpki.server_status()["warnings"])
    monkeypatch.setattr(webpki, "lan_ip", lambda: "192.168.1.20")
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=380)
    st = webpki.server_status(now=later)
    assert st["days_left"] < 30 and any("expires" in w for w in st["warnings"])


def test_ca_is_name_constrained(pki):
    ca = x509.load_pem_x509_certificate((pki / "ca.crt").read_bytes())
    ext = ca.extensions.get_extension_for_class(x509.NameConstraints)
    assert ext.critical
    permitted = {str(g.value) for g in ext.value.permitted_subtrees}
    assert {"local", "localhost", "192.168.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "127.0.0.0/8"} <= permitted


def test_ca_key_is_encrypted_with_keychain_passphrase(pki, keychain):
    assert b"ENCRYPTED PRIVATE KEY" in (pki / "ca.key").read_bytes()
    assert list(keychain) == [str(pki)] and len(keychain[str(pki)]) >= 32
    webpki.enroll_pem("mac")
    keychain.clear()
    with pytest.raises(SystemExit, match="Keychain"):
        webpki.enroll_pem("other")


def test_protect_ca_migrates_plaintext_key_without_changing_the_ca(pki, keychain):
    from cryptography.hazmat.primitives import serialization
    key = webpki._load_ca()[0]
    (pki / "ca.key").write_bytes(webpki._pem_key(key))
    keychain.clear()
    ca = (pki / "ca.crt").read_bytes()
    assert webpki.protect_ca() is True
    assert b"ENCRYPTED PRIVATE KEY" in (pki / "ca.key").read_bytes()
    assert webpki.protect_ca() is False
    again = webpki._load_ca()[0]
    pub = lambda k: k.public_key().public_bytes(serialization.Encoding.DER,  # noqa: E731
                                                  serialization.PublicFormat.SubjectPublicKeyInfo)
    assert pub(again) == pub(key) and (pki / "ca.crt").read_bytes() == ca
    assert stat.S_IMODE((pki / "ca.key").stat().st_mode) == 0o600
