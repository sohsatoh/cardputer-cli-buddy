"""Wi-Fi（TCP）のリンク（PROTOCOL.md の「Wi-Fi（TCP）」「BLE と Wi-Fi の切り替え」）。

/flash/cardbuddy_wifi.json があれば Wi-Fi に接続し、UDP のビーコンで buddyd を見つけて TCP でつなぐ。
メインループから service() を毎回呼ぶ。どの処理もブロックしない。
送受信の形は buddy_ble と同じ（send_line / enqueue / pump / tx_idle / hello_failed）。
"""

import json

import select
import socket

import buddy_protocol
from buddy_protocol import ms, since

try:
    import esp32
except ImportError:  # CPython（テスト）では IDF ヒープの判定をしない
    esp32 = None

CFG_PATH = "/flash/cardbuddy_wifi.json"
BEACON_PORT = 47824
MAX_LINE = 4096
JOIN_MS = 15000
CONNECT_MS = 5000
IDLE_MS = 30000
BAN_MS = 60000
_RECV = 512  # 空きメモリが少ないので受信は小分けにする
# IDF ヒープの空きがこれを切ったら TCP に書かない。lwIP と Wi-Fi の送信バッファに上限が無く、
# 書き続けると IDF ヒープが尽きて ETIMEDOUT や panic になる（実機で確認）
IDF_MIN_FREE = 8 * 1024
# 合計が足りていても断片化して連続領域が無いと、Wi-Fi / lwIP のバッファ（約 1.6KB）が取れない
IDF_MIN_BLOCK = 4 * 1024
_EAGAIN = 11
_EINPROGRESS = (115, 119)


# しきい値を切っても、下限までは TRICKLE byte ずつ送る。Wi-Fi は受信の後などに IDF ヒープを数十秒抱え、
# 空きがしきい値の少し下で止まることがある（実機で 7.8KB）。そこで全部止めると、buddyd が無通信で切り、
# 再接続の Hello も送れなくなる
IDF_FLOOR_FREE = 4 * 1024
IDF_FLOOR_BLOCK = 2 * 1024
TRICKLE = 512


def _idf_room():
    """今 TCP に書いてよい byte 数。None なら制限なし、0 なら書かない。"""
    if esp32 is None:
        return None
    free = big = 0
    for h in esp32.idf_heap_info(esp32.HEAP_DATA):
        free += h[1]
        if h[2] > big:
            big = h[2]
    if free >= IDF_MIN_FREE and big >= IDF_MIN_BLOCK:
        return None
    if free >= IDF_FLOOR_FREE and big >= IDF_FLOOR_BLOCK:
        return TRICKLE
    return 0


def arbitrate(proto, ble, wifi):
    """Wi-Fi のセッションがある間は BLE の広告を止め、接続も切る。無くなったら広告を再開する。"""
    if proto.link is wifi and wifi is not None:
        if not ble.paused or ble.connected:
            ble.pause()
    elif ble.paused:
        ble.resume()


class WifiLink:
    kind = "Wi-Fi"

    def __init__(self, proto, path=CFG_PATH):
        self.p = proto
        self.state = "off"  # off / join / scan / tcp / open
        self._sta = None
        self._udp = None
        self._sock = None
        self._poll = None
        self._peer = None
        self._t0 = 0
        self._last_rx = 0
        self._rx = b""
        self._skip = False
        self._ban = {}
        self._tx = buddy_protocol.TxQueue(self._write)
        try:
            with open(path) as f:
                cfg = json.load(f)
            self._ssid, self._psk = cfg["ssid"], cfg.get("psk", "")
        except (OSError, ValueError, KeyError, TypeError):
            return
        print("wifi: config for ssid", self._ssid)
        self.state = "join"

    @property
    def connected(self):
        return self.state == "open"

    # ----- 状態機械

    def service(self):
        st = self.state
        if st == "off":
            return
        if st == "join":
            self._service_join()
            return
        if not self._sta.isconnected():
            print("wifi: lost")
            self._close("wifi lost", "join")
            self._join()
            return
        if st == "scan":
            self._service_scan()
        elif st == "tcp":
            self._service_tcp()
        elif st == "open":
            self._service_open()

    def _join(self):
        import network

        if self._sta is None:
            self._sta = network.WLAN(network.STA_IF)
            self._sta.active(True)
        # 起動時の自動接続などが裏で再接続を続けていると connect が Internal Error になるので、必ず先に切る
        try:
            self._sta.disconnect()
        except OSError:
            pass
        try:
            self._sta.connect(self._ssid, self._psk)
        except OSError as e:
            print("wifi: connect error", e)
        self._t0 = ms()

    def _service_join(self):
        if self._sta is None:
            self._join()
            return
        if self._sta.isconnected():
            print("wifi: up", self._sta.ifconfig()[0])
            self._open_udp()
        elif since(self._t0) > JOIN_MS:
            print("wifi: join timed out, retry")
            self._join()

    def _open_udp(self):
        u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            u.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        except (OSError, AttributeError):
            pass
        u.bind(("0.0.0.0", BEACON_PORT))
        u.setblocking(False)
        self._udp = u
        self.state = "scan"

    def _service_scan(self):
        while True:
            try:
                data, addr = self._udp.recvfrom(64)
            except OSError:
                return
            ip = addr[0] if isinstance(addr, tuple) else None
            port = 0
            if ip and data.startswith(b"cardbuddy/1 "):
                try:
                    port = int(data[12:])
                except ValueError:
                    pass
            if not 0 < port < 65536:
                continue
            t = self._ban.get(ip)
            if t is not None:
                if since(t) < BAN_MS:
                    continue
                del self._ban[ip]
            self._connect(ip, port)
            return

    def _connect(self, ip, port):
        print("wifi: connect", ip, port)
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setblocking(False)
        try:
            s.connect((ip, port))
        except OSError as e:
            if not (e.args and e.args[0] in _EINPROGRESS):
                print("wifi: connect failed", e)
                s.close()
                return
        self._udp.close()
        self._udp = None
        self._sock, self._peer = s, ip
        self._poll = select.poll()
        self._poll.register(s, select.POLLOUT)
        self._t0 = ms()
        self.state = "tcp"

    def _events(self):
        ev = self._poll.poll(0)
        return ev[0][1] if ev else 0

    def _service_tcp(self):
        ev = self._events()
        if ev & (select.POLLERR | select.POLLHUP):
            self._close("tcp refused")
        elif ev & select.POLLOUT:
            self._poll.modify(self._sock, select.POLLIN)
            try:
                self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except (OSError, AttributeError):
                pass
            self._rx, self._skip = b"", False
            self._last_rx = ms()
            self.state = "open"
            print("wifi: tcp open")
        elif since(self._t0) > CONNECT_MS:
            self._close("tcp connect timed out")

    def _service_open(self):
        ev = self._events()
        if ev & select.POLLIN:
            try:
                data = self._sock.recv(_RECV)
            except OSError as e:
                if not (e.args and e.args[0] == _EAGAIN):
                    self._close("recv failed")
                    return
                data = None
            if data == b"":
                self._close("peer closed")
                return
            if data:
                self._last_rx = ms()
                self._feed(data)
                if self.state != "open":
                    return
        elif ev & (select.POLLERR | select.POLLHUP):
            self._close("tcp error")
            return
        if since(self._last_rx) > IDLE_MS:
            self._close("idle")
            return
        if self._tx.pump() is None:
            self._close("send failed")

    def _feed(self, data):
        buf = self._rx + data
        while True:
            i = buf.find(b"\n")
            if i < 0:
                break
            line, buf = buf[:i], buf[i + 1 :]
            if self._skip or len(line) > MAX_LINE:
                self._skip = False
                print("wifi: drop over-long line")
                continue
            self.p.on_line(line, self)
            if self.state != "open":
                return
        if len(buf) > MAX_LINE:
            print("wifi: drop over-long line")
            buf, self._skip = b"", True
        self._rx = buf

    def _close(self, why, then="scan"):
        print("wifi: close:", why)
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._udp is not None:
            self._udp.close()
        self._sock = self._udp = self._poll = None
        self._rx = b""
        self._tx.clear()
        self.state = then
        self.p.on_disconnect(self)
        if then == "scan":
            self._open_udp()

    # ----- リンクとしての口（buddy_ble と同じ形）

    def hello_failed(self):
        if self._peer is not None:
            self._ban[self._peer] = ms()
        if self.state == "open":
            self._close("hello failed")

    def _write(self, mv):
        if self._sock is None:
            raise OSError(107)  # ENOTCONN
        room = _idf_room()
        if room == 0:
            return 0
        if room is not None:
            mv = mv[:room]
        try:
            return self._sock.send(mv)
        except OSError as e:
            if e.args and e.args[0] == _EAGAIN:
                return 0
            raise

    def send_line(self, payload):
        if self.state != "open":
            return False
        if not payload.endswith(b"\n"):
            payload = payload + b"\n"
        return self._tx.send(payload)

    def enqueue(self, line):
        return self._tx.put(line)

    def tx_idle(self):
        return self._tx.idle()

    def pump(self):
        return self._tx.pump()
