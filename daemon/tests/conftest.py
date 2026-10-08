import pytest

from cardbuddy import webpki


@pytest.fixture(autouse=True)
def keychain(monkeypatch):
    """テストから実際の macOS Keychain に書き込まない。"""
    store = {}
    monkeypatch.setattr(webpki, "keychain_get", lambda acct: store.get(acct))
    monkeypatch.setattr(webpki, "keychain_set", lambda acct, pw: store.__setitem__(acct, pw))
    return store
