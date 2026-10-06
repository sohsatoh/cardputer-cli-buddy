"""Cardputer CLI Buddy の本体: buddyd と BLE / Wi-Fi でつなぎ、Claude Code の perm / ask に答え、プロンプトを送る。

ランチャーから起動される apps/claude_buddy.py は、これを import して run() を呼ぶだけの小さな入口にしている。
.py を import 時にコンパイルすると、その作業領域で GC ヒープが IDF ヒープを取って伸びるので、本体は .mpy で入れる。

プロトコルは PROTOCOL.md、鍵は /flash/cardbuddy.key（`buddy pair` が USB で書く）。
画面とキー操作は buddy_ui_cp、暗号とメッセージは buddy_protocol / crypto。

### キー

「↑↓」は単体の ; . と Fn+↑↓ のどちらでもよい。Esc は ` の位置のキー。

  一覧    1-9 / ↑↓ で選択、Enter で入力、l か Fn+→ でログ、Q で終了
  ログ    ↑↓ でスクロール、r で再要求、Enter で入力、Esc で一覧へ
  perm    Y allow / N deny、内容が長いときは ↑↓ でスクロール
  ask     1-4 / ↑↓ で選択、Space で複数選択のトグル、Enter で確定
  入力    Fn+←→ でカーソル移動、Fn+↑↓ で行移動、Del でカーソル前を消す、
          Enter で送信、Esc で取り消し（; , . / は文字として入る）、
          Tab で 英数 / ひらがな / カタカナ を切り替え（ローマ字かな変換、漢字変換はしない）
  音声    一覧で v、Enter / Space で録音の開始と停止、Tab で ja-JP / en-US、Esc で取り消し、
          認識結果は入力画面に入る（確認・編集して Enter で送る）

### 接続

BLE で待ち受けつつ、/flash/cardbuddy_wifi.json があれば Wi-Fi にも接続し、buddyd のビーコンを
待って TCP でつなぐ（wifi_link）。Wi-Fi のセッションが確立したら BLE の広告を止める。
ヘッダの右に接続の種類（BLE / Wi-Fi）を出す。

### 終了

UIFlow 2.0 にはランチャーへ戻る API が無いので、upstream の他アプリと同じく
`machine.reset()` で再起動して戻る。
"""

import sys

# ランチャーのアニメーション（約 10KB）は、アプリの起動後は使わないので手放す。
# GC ヒープに空きが無いと、伸びるときに IDF ヒープの最大ブロックを丸ごと取り、Wi-Fi のメモリが尽きる
sys.modules.pop("burst_frames", None)
if "launcher" in sys.modules:
    sys.modules["launcher"]._burst = None

import voice

# 音声のバッファ（約 21KB）は、他のモジュールの import より前に確保する。後で確保すると GC ヒープが
# IDF ヒープの最大ブロック（約 32KB）を取って伸び、Wi-Fi のメモリが尽きる（実機で確認）
_VOICE_BUFS = voice.alloc()

import time

import M5
import machine
from hardware import MatrixKeyboard

import buddy_ble
import buddy_protocol
import buddy_ui_cp as buddy_ui
import wifi_link


def _mem(tag):
    import gc

    gc.collect()
    try:
        import esp32

        h = esp32.idf_heap_info(esp32.HEAP_DATA)
        print("claude_buddy: mem", tag, "gc", gc.mem_free(), "idf", sum(x[1] for x in h), max(x[2] for x in h))
    except ImportError:
        print("claude_buddy: mem", tag, "gc", gc.mem_free())


def run():
    print("claude_buddy: run() start")
    _mem("start")

    # WiFi が動いたままだと、無線を共有する NimBLE の active(True) が C レベルで落ちることがある、
    # 待ちの 1000 ms も同じ理由で upstream の実機調整値
    try:
        import network

        sta = network.WLAN(network.STA_IF)
        if sta.active():
            try:
                sta.disconnect()
            except OSError:
                pass
            sta.active(False)
    except Exception as e:
        print("claude_buddy: wifi disable warning:", e)
    time.sleep_ms(1000)

    ble = None
    # IRQ 中の SPI 描画は LCD を崩すので、IRQ では行をためるだけにして、
    # 復号と描画はメインループでやる
    inbox = []
    conn = ["advertising", False]  # 最新の接続状態、切断を見たか

    def on_line(line):
        buddy_protocol.inbox_push(inbox, line)

    def on_state(s):
        print("claude_buddy: state", s)
        conn[0] = s
        if s == "disconnected":
            conn[1] = True

    proto = buddy_protocol.Protocol(buddy_protocol.load_key())
    print("claude_buddy: paired =", proto.paired)
    ui = buddy_ui.BuddyUI(proto)
    ui.refresh()

    import gc

    # このアプリは音を出さない。Speaker は起動時に有効で、IDF ヒープを約 7.7KB 使っている
    M5.Speaker.end()
    gc.collect()
    ble = buddy_ble.BuddyBLE(on_line=on_line, on_state=on_state)
    print("CLI Buddy up as", ble.advertised_name)
    _mem("ble")
    vo = voice.Voice(proto, bufs=_VOICE_BUFS)
    ui.voice = vo
    _mem("ready")
    # BLE のスタックは上で 1 回だけ立ち上げ、以後は止めない。Wi-Fi はその後に並行して使う
    wifi = wifi_link.WifiLink(proto)

    # 起動に使ったキーを拾わないための待ち（upstream の他アプリと同じ）
    kb = MatrixKeyboard()
    time.sleep_ms(400)

    try:
        while True:
            if conn[1]:
                conn[1] = False
                inbox.clear()
                proto.on_disconnect(ble)
            while inbox:
                proto.on_line(inbox.pop(0), ble)
            wifi.service()
            proto.tick()
            wifi_link.arbitrate(proto, ble, wifi)
            ui.set_connection(conn[0])

            kb.tick()
            k = kb.get_key()
            if k is not None and ui.on_key(k) == "quit":
                return
            vo.service(proto.link or ble)
            ble.pump()
            wifi.pump()
            ui.refresh()
            # 録音中や送信待ちがある間は、送信を詰まらせないよう短い間隔で回す
            busy = vo.state in ("rec", "flush") or not ble.tx_idle() or not wifi.tx_idle()
            time.sleep_ms(5 if busy else 40)
    except Exception as e:
        # finally の reset が先に走ると traceback が出ないまま再起動し、原因が追えないので先に出す
        sys.print_exception(e)
        try:
            M5.Lcd.fillScreen(buddy_ui.BLACK)
            M5.Lcd.setTextColor(buddy_ui.RED, buddy_ui.BLACK)
            M5.Lcd.drawString("Error: " + repr(e)[:40], 6, 40)
        except Exception:
            pass
        time.sleep_ms(3000)
    finally:
        try:
            vo.close()
        except Exception as e:
            print("claude_buddy: voice close warning:", e)
        try:
            ble.deinit()
        except Exception as e:
            print("claude_buddy: deinit warning:", e)
        try:
            M5.Lcd.fillScreen(buddy_ui.BLACK)
        except Exception as e:
            print("claude_buddy: screen-clear warning:", e)
        time.sleep_ms(200)
        machine.reset()

