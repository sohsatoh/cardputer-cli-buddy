"""UIFlow 2.0 の boot.py が起動するランチャー（boot_option=2）。本体は launcher（.mpy）。

ランチャーを .py のまま起動時にコンパイルすると、その作業領域で GC ヒープが IDF ヒープを取って伸び、
後で動かすアプリ（BLE + Wi-Fi）のメモリが足りなくなる（実機で確認）。
"""

import launcher  # noqa: F401  import 時にメニューを動かす
