"""wifi_link（Wi-Fi への接続、UDP ビーコン、TCP、Hello の失敗時の除外、タイムアウト）を偽物で検証する。"""

import json
import sys
import time
import types

import pytest

from test_device_protocol import KEY, NH, Dev, host

POLLIN, POLLOUT, POLLERR, POLLHUP = 1, 4, 8, 16


class FakeWLAN:
    def __init__(self, log):
        self.log = log
        self.up = False

    def active(self, on=None):
        self.log.append(("active", on))

    def disconnect(self):
        self.log.append(("disconnect",))

    def connect(self, ssid, psk):
        self.log.append(("connect", ssid, psk))

    def isconnected(self):
        return self.up

    def ifconfig(self):
        return ("192.168.1.20", "255.255.255.0", "192.168.1.1", "192.168.1.1")


class FakeSock:
    def __init__(self, net, kind):
        self.net, self.kind = net, kind
        self.closed = False
        self.datagrams = []
        self.inbound = b""
        self.out = b""
        self.state = "new"  # tcp: new / connecting / up / refused / eof
        self.cap = 10_000
        self.bound = None
        net.socks.append(self)

    def setsockopt(self, *a):
        pass

    def setblocking(self, flag):
        assert flag is False

    def bind(self, addr):
        self.bound = addr

    def recvfrom(self, n):
        if not self.datagrams:
            raise OSError(11)
        return self.datagrams.pop(0)

    def connect(self, addr):
        self.addr = addr
        self.state = "connecting"
        raise OSError(119)  # EINPROGRESS

    def send(self, mv):
        if self.cap == 0:
            raise OSError(11)
        n = min(len(mv), self.cap)
        self.out += bytes(mv[:n])
        return n

    def recv(self, n):
        if self.state == "eof" and not self.inbound:
            return b""
        if not self.inbound:
            raise OSError(11)
        data, self.inbound = self.inbound[:n], self.inbound[n:]
        return data

    def close(self):
        self.closed = True

    def events(self):
        if self.state == "refused":
            return POLLERR
        ev = POLLOUT if self.state in ("up", "eof") else 0
        if self.inbound or self.state == "eof":
            ev |= POLLIN
        return ev


class FakePoll:
    def __init__(self):
        self.socks = {}

    def register(self, s, mask):
        self.socks[id(s)] = (s, mask)

    def modify(self, s, mask):
        self.socks[id(s)] = (s, mask)

    def unregister(self, s):
        self.socks.pop(id(s), None)

    def poll(self, timeout):
        out = []
        for s, mask in self.socks.values():
            ev = s.events() & (mask | POLLERR | POLLHUP)
            if ev:
                out.append((s, ev))
        return out


class Net:
    def __init__(self):
        self.log = []
        self.wlan = FakeWLAN(self.log)
        self.socks = []

    def udp(self):
        return [s for s in self.socks if s.kind == "udp" and not s.closed]

    def tcp(self):
        return [s for s in self.socks if s.kind == "tcp"]


@pytest.fixture
def env(monkeypatch, tmp_path):
    net = Net()
    clock = [0]
    monkeypatch.setitem(sys.modules, "network", types.SimpleNamespace(STA_IF=0, WLAN=lambda i: net.wlan))
    monkeypatch.setattr(time, "ticks_ms", lambda: clock[0], raising=False)
    monkeypatch.setattr(time, "ticks_diff", lambda a, b: a - b, raising=False)
    monkeypatch.setattr(time, "sleep_ms", lambda ms: None, raising=False)
    monkeypatch.delitem(sys.modules, "wifi_link", raising=False)
    import wifi_link

    sockmod = types.SimpleNamespace(
        AF_INET=2, SOCK_DGRAM=2, SOCK_STREAM=1, SOL_SOCKET=1, SO_REUSEADDR=4, IPPROTO_TCP=6, TCP_NODELAY=1,
        socket=lambda af=2, kind=1: FakeSock(net, "udp" if kind == 2 else "tcp"),
    )
    monkeypatch.setattr(wifi_link, "socket", sockmod)
    monkeypatch.setattr(wifi_link, "select", types.SimpleNamespace(
        poll=FakePoll, POLLIN=POLLIN, POLLOUT=POLLOUT, POLLERR=POLLERR, POLLHUP=POLLHUP))
    cfg = tmp_path / "wifi.json"
    cfg.write_text(json.dumps({"ssid": "home", "psk": "s3cret-psk"}))
    d = Dev()
    return wifi_link, net, clock, d, str(cfg)


def up_to_scan(wl, net, clock):
    wl.service()
    net.wlan.up = True
    clock[0] += 1200
    wl.service()
    assert wl.state == "scan"
    (u,) = net.udp()
    return u


def beacon(u, ip="192.168.1.5", port=47823):
    u.datagrams.append((b"cardbuddy/1 %d" % port, (ip, 50000)))


def open_tcp(wl, net, clock, ip="192.168.1.5"):
    u = net.udp()[0]
    beacon(u, ip)
    wl.service()
    s = net.tcp()[-1]
    assert wl.state == "tcp" and s.addr == (ip, 47823)
    s.state = "up"
    wl.service()
    assert wl.state == "open"
    return s


def hello(wl, s, key=KEY, nh=NH):
    s.inbound += host.hello(host.ROLE_HOST, nh)
    wl.service()
    line, s.out = s.out, b""
    nd = host.parse_hello_device(line, KEY, nh)
    s.inbound += host.hello_ack(key, nh, nd)
    wl.service()
    return host.Session(*host.hkdf(KEY, nh, nd), host.DIR_H2D)


def test_no_config_stays_off(env, tmp_path):
    wifi_link, net, clock, d, _ = env
    wl = wifi_link.WifiLink(d.p, str(tmp_path / "missing.json"))
    wl.service()
    assert wl.state == "off" and net.log == []


def test_join_disconnects_first_and_never_logs_psk(env, capsys):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    wl.service()
    assert net.log == [("active", True), ("disconnect",), ("connect", "home", "s3cret-psk")]
    up_to_scan(wl, net, clock)
    assert net.udp()[0].bound == ("0.0.0.0", 47824)
    assert "s3cret-psk" not in capsys.readouterr().out


def test_join_retries_after_timeout(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    wl.service()
    clock[0] += 16000
    wl.service()
    assert [x[0] for x in net.log].count("connect") == 2 and wl.state == "join"


def test_beacon_tcp_hello_and_session(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    net.udp()[0].datagrams.append((b"garbage", ("192.168.1.9", 1)))
    s = open_tcp(wl, net, clock)
    assert not net.udp()  # 接続中はビーコンを聞かない
    rx = hello(wl, s)
    assert d.p.ready and d.p.link is wl
    s.inbound += rx.seal({"t": "ping"})
    wl.service()
    assert rx.open(s.out) == {"t": "pong"}


def test_lines_split_across_reads_and_overlong_dropped(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    rx = hello(wl, s)
    s.inbound += b"A" * 5000 + b"\n"  # 上限超えの行は捨てる
    line = rx.seal({"t": "ping"})
    for i in range(0, len(line), 7):
        s.inbound += line[i : i + 7]
        wl.service()
    while s.inbound:
        wl.service()
    assert rx.open(s.out) == {"t": "pong"} and wl.state == "open"


def test_partial_tcp_writes_resume(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    rx = hello(wl, s)
    s.cap = 0
    wl.enqueue(b"x" * 100 + b"\n")
    assert wl.pump() is False
    s.cap = 30
    wl.service()
    wl.service()
    wl.service()
    wl.service()
    assert s.out == b"x" * 100 + b"\n" and wl.tx_idle()
    assert rx is not None


def test_bad_hello_bans_peer_for_60s(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    hello(wl, s, key=bytes(32))
    assert s.closed and wl.state == "scan" and not d.p.ready
    beacon(net.udp()[0])
    wl.service()
    assert wl.state == "scan" and len(net.tcp()) == 1  # 60 秒間は候補から外す
    beacon(net.udp()[0], ip="192.168.1.6")
    wl.service()
    assert wl.state == "tcp" and net.tcp()[-1].addr[0] == "192.168.1.6"


def test_ban_expires(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    hello(wl, s, key=bytes(32))
    clock[0] += 61000
    open_tcp(wl, net, clock)


def test_hello_timeout_closes_and_bans(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    s.inbound += host.hello(host.ROLE_HOST, NH)
    wl.service()
    clock[0] += 5100
    d.p.tick()
    assert s.closed and wl.state == "scan"
    beacon(net.udp()[0])
    wl.service()
    assert wl.state == "scan"


def test_idle_30s_disconnects_and_drops_session(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    rx = hello(wl, s)
    clock[0] += 29000
    wl.service()
    s.inbound += rx.seal({"t": "ping"})
    wl.service()
    clock[0] += 29000
    wl.service()
    assert wl.state == "open"
    clock[0] += 2000
    wl.service()
    assert s.closed and wl.state == "scan" and not d.p.ready and d.p.link is None


def test_peer_close_and_refused_connect(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    s.state = "eof"
    wl.service()
    assert s.closed and wl.state == "scan"
    beacon(net.udp()[0])
    wl.service()
    net.tcp()[-1].state = "refused"
    wl.service()
    assert wl.state == "scan" and net.tcp()[-1].closed


def test_tcp_connect_timeout(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    beacon(net.udp()[0])
    wl.service()
    clock[0] += 5100
    wl.service()
    assert wl.state == "scan" and net.tcp()[-1].closed


def test_wifi_drop_goes_back_to_join(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    hello(wl, s)
    net.wlan.up = False
    wl.service()
    assert s.closed and wl.state == "join" and not d.p.ready


class FakeBle:
    def __init__(self):
        self.paused = False
        self.connected = True
        self.calls = []

    def pause(self):
        self.paused = True
        self.connected = False
        self.calls.append("pause")

    def resume(self):
        self.paused = False
        self.calls.append("resume")


def test_arbitrate_pauses_ble_for_wifi_session_and_resumes(env):
    wifi_link, net, clock, d, cfg = env
    wl = wifi_link.WifiLink(d.p, cfg)
    ble = FakeBle()
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    wifi_link.arbitrate(d.p, ble, wl)
    assert ble.calls == []  # 確立前は BLE のまま
    hello(wl, s)
    wifi_link.arbitrate(d.p, ble, wl)
    wifi_link.arbitrate(d.p, ble, wl)
    assert ble.calls == ["pause"]
    ble.connected = True  # 止めている間につながってきた central も切る
    wifi_link.arbitrate(d.p, ble, wl)
    assert ble.calls == ["pause", "pause"]
    s.state = "eof"
    wl.service()
    wifi_link.arbitrate(d.p, ble, wl)
    assert ble.calls[-1] == "resume" and not ble.paused


class FakeEsp32:
    HEAP_DATA = 4

    def __init__(self):
        self.free = [50_000, 20_000]

    def idf_heap_info(self, kind):
        assert kind == self.HEAP_DATA
        return [(100_000, f, f, f) for f in self.free]


def test_tcp_send_waits_while_idf_heap_is_low(env, monkeypatch):
    wifi_link, net, clock, d, cfg = env
    esp = FakeEsp32()
    monkeypatch.setattr(wifi_link, "esp32", esp)
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    rx = hello(wl, s)
    esp.free = [3_000, 500]  # 合計が下限未満
    wl.enqueue(b"y" * 50 + b"\n")
    wl.service()
    assert s.out == b"" and not wl.tx_idle() and wl.state == "open"
    esp.free = [6_000, wifi_link.IDF_MIN_FREE]
    wl.service()
    assert s.out == b"y" * 50 + b"\n" and wl.tx_idle()
    assert rx is not None


def test_tcp_send_trickles_while_idf_heap_is_short(env, monkeypatch):
    """空きが下限としきい値の間なら、小分けにして送り続ける。

    Wi-Fi は受信の後などに IDF ヒープを数十秒抱え、空きが 8KB の少し下で止まることがある（実機）。
    そこで全部止めると、buddyd が 40 秒で切り、再接続の Hello も送れなくなる。
    """
    wifi_link, net, clock, d, cfg = env
    esp = FakeEsp32()
    monkeypatch.setattr(wifi_link, "esp32", esp)
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    hello(wl, s)
    sizes = []
    send = s.send

    def spy(mv):
        sizes.append(len(mv))
        return send(mv)

    s.send = spy
    esp.free = [wifi_link.IDF_FLOOR_FREE, 2_000, 1_900]  # 合計はしきい値未満、下限以上
    line = b"w" * 3000 + b"\n"
    wl.enqueue(line)
    wl.service()
    assert s.out == line and wl.tx_idle()
    assert max(sizes) <= wifi_link.TRICKLE
    sizes.clear()
    esp.free = [wifi_link.IDF_FLOOR_BLOCK - 1] * 5  # 合計はあるが、連続した領域が下限未満
    wl.enqueue(b"v" * 10 + b"\n")
    wl.service()
    assert sizes == [] and not wl.tx_idle()


def test_tcp_send_waits_while_idf_heap_is_fragmented(env, monkeypatch):
    wifi_link, net, clock, d, cfg = env
    esp = FakeEsp32()
    monkeypatch.setattr(wifi_link, "esp32", esp)
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    hello(wl, s)
    esp.free = [1_500] * 10  # 合計は多いが、連続した領域が小さい
    wl.enqueue(b"z" * 50 + b"\n")
    wl.service()
    assert s.out == b""
    esp.free = [1_500] * 9 + [wifi_link.IDF_MIN_BLOCK]
    wl.service()
    assert s.out == b"z" * 50 + b"\n"


def test_ping_is_answered_while_recording_without_blocking(env, monkeypatch, tmp_path):
    wifi_link, net, clock, d, cfg = env
    import test_device_voice as tv

    log = []
    mic = tv.FakeMic(log)
    monkeypatch.setitem(sys.modules, "M5", types.SimpleNamespace(Mic=mic, Speaker=tv.FakeSpeaker(log)))
    monkeypatch.delitem(sys.modules, "voice", raising=False)
    import voice

    monkeypatch.setattr(voice, "SPOOL", str(tmp_path / "voice.tmp"))
    slept = []
    monkeypatch.setattr(time, "sleep_ms", lambda ms: (slept.append(ms), clock.__setitem__(0, clock[0] + ms)), raising=False)
    wl = wifi_link.WifiLink(d.p, cfg)
    up_to_scan(wl, net, clock)
    s = open_tcp(wl, net, clock)
    rx = hello(wl, s)
    v = voice.Voice(d.p)
    assert v.start("ja-JP")
    s.cap = 0  # 送信が詰まっている（音声の行がキューに残る）
    for i in range(25):  # 2.5 秒分の録音
        mic.finish()
        v.service(wl)
        if i == 10:
            s.inbound += rx.seal({"t": "ping"})
        wl.service()
        clock[0] += 100
    assert slept == []  # pong の送信でメインループを止めない
    assert wl.state == "open" and since_rx(wl, clock) < 2000
    s.cap = 10_000
    for _ in range(10):
        wl.service()
        v.service(wl)
    kinds = []
    for line in s.out.split(b"\n")[:-1]:
        kinds.append(rx.open_frame(line + b"\n"))
    assert {"t": "voice_begin", "vid": v.vid, "lang": "ja-JP"} in [m for k, m in kinds if k == host.DATA]
    assert {"t": "pong"} in [m for k, m in kinds if k == host.DATA]


def since_rx(wl, clock):
    return clock[0] - wl._last_rx
