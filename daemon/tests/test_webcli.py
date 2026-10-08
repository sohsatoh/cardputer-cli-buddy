import plistlib
import shutil
import stat
import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.serialization import pkcs12

from cardbuddy import cli, webpki


@pytest.fixture
def home(monkeypatch):
    d = Path(tempfile.mkdtemp(prefix="cb"))
    monkeypatch.setenv("CARDBUDDY_HOME", str(d / "h"))
    monkeypatch.setattr(webpki, "lan_ip", lambda: "192.168.1.20")
    monkeypatch.setattr(webpki, "local_hostname", lambda: "macbook")
    yield d
    shutil.rmtree(d)


def test_web_init_devices_enroll_revoke(home, capsys, monkeypatch):
    assert cli.main(["web", "init"]) == 0
    out = capsys.readouterr().out
    assert "macbook.local" in out and "192.168.1.20" in out
    with pytest.raises(SystemExit):
        cli.main(["web", "init"])

    tty = []
    monkeypatch.setattr(cli, "_to_terminal", tty.append)
    assert cli.main(["web", "enroll", "iphone", "--out", str(home / "dl")]) == 0
    out = capsys.readouterr().out
    profile = home / "dl" / "cardbuddy-iphone.mobileconfig"
    assert str(profile) in out and stat.S_IMODE(profile.stat().st_mode) == 0o600
    (line,) = tty
    password = line.split()[-1]
    assert password not in out
    ident = plistlib.loads(profile.read_bytes())["PayloadContent"][1]
    pkcs12.load_pkcs12(ident["PayloadContent"], password.encode())

    assert cli.main(["web", "enroll", "mac", "--pem"]) == 0
    out = capsys.readouterr().out
    assert "client-mac.crt" in out and "curl" in out

    assert cli.main(["web", "devices"]) == 0
    out = capsys.readouterr().out
    assert "iphone" in out and "mac" in out and "https://macbook.local:47825/" in out
    assert "--renew" not in out

    monkeypatch.setattr(webpki, "lan_ip", lambda: "10.0.0.9")
    assert cli.main(["web", "devices"]) == 0
    assert "--renew" in capsys.readouterr().out
    assert cli.main(["web", "init", "--renew"]) == 0
    capsys.readouterr()
    assert cli.main(["web", "devices"]) == 0
    assert "--renew" not in capsys.readouterr().out

    assert cli.main(["web", "revoke", "iphone"]) == 0
    assert cli.main(["web", "revoke", "iphone"]) == 1
    capsys.readouterr()
    assert cli.main(["web", "devices"]) == 0
    assert "iphone" not in capsys.readouterr().out.split("server")[0]


def test_web_commands_need_init(home):
    with pytest.raises(SystemExit, match="buddy web init"):
        cli.main(["web", "enroll", "x", "--pem"])
