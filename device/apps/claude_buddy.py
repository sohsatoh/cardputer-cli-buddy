"""Cardputer CLI Buddy: buddyd と BLE でつなぎ、Claude Code の perm / ask に答え、プロンプトを送る。

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

### 終了

UIFlow 2.0 にはランチャーへ戻る API が無いので、upstream の他アプリと同じく
`machine.reset()` で再起動して戻る。
"""

import sys

# /flash は UIFlow 2.0 の既定 sys.path に無い
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import time

import M5
import machine
from hardware import MatrixKeyboard

import buddy_ble
import buddy_protocol
import buddy_ui_cp as buddy_ui


def run():
    print("claude_buddy: run() start")

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

    def send_line(line):
        return ble.send_line(line) if ble is not None else False

    proto = buddy_protocol.Protocol(buddy_protocol.load_key(), send_line)
    print("claude_buddy: paired =", proto.paired)
    ui = buddy_ui.BuddyUI(proto)
    ui.refresh()

    import gc

    gc.collect()
    ble = buddy_ble.BuddyBLE(on_line=on_line, on_state=on_state)
    print("CLI Buddy up as", ble.advertised_name)

    # 起動に使ったキーを拾わないための待ち（upstream の他アプリと同じ）
    kb = MatrixKeyboard()
    time.sleep_ms(400)

    try:
        while True:
            if conn[1]:
                conn[1] = False
                inbox.clear()
                proto.on_disconnect()
            while inbox:
                proto.on_line(inbox.pop(0))
            ui.set_connection(conn[0])

            kb.tick()
            k = kb.get_key()
            if k is not None and ui.on_key(k) == "quit":
                return
            ui.refresh()
            time.sleep_ms(40)
    finally:
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


# UIFlow の App List は __main__ としても import としても呼ぶので、無条件に run する
run()
