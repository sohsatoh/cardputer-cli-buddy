"""UIFlow 2.0 の boot.py が起動する入口（boot_option=2）。Cardputer CLI Buddy を直接動かす。本体は buddy_app（.mpy）。

NimBLE は、他のモジュールを import する前に有効にする。import の後だと GC ヒープが先に伸びて、
BLE を有効にした後の IDF ヒープが 13KB しか残らず、Wi-Fi の接続後に落ちた（実機。先に有効にすると約 46KB）。
"""

import sys
import time

import bluetooth

bluetooth.BLE().active(True)
time.sleep_ms(250)

if "/flash" not in sys.path:
    sys.path.insert(0, "/flash")

import buddy_app  # noqa: E402

buddy_app.run()
