"""Cardputer CLI Buddy の入口（ランチャーの App List に出る）。本体は buddy_app（.mpy）。"""

import sys

# /flash は UIFlow 2.0 の既定 sys.path に無い
for _p in ("/flash", "/flash/apps"):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import buddy_app

# UIFlow の App List は __main__ としても import としても呼ぶので、無条件に run する
buddy_app.run()
