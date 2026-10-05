"""testvectors/envelope.json を生成する。実行: cd daemon && uv run python ../testvectors/gen.py

入力（鍵・nonce）は固定値なので、何度実行しても同じ出力になる。
"""

import base64
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "daemon"))
from cardbuddy import crypto as c  # noqa: E402

KEY = bytes(range(32))
NH = bytes([0xA0 + i for i in range(16)])
ND = bytes([0xB0 + i for i in range(16)])
ND_OLD = bytes([0xC0 + i for i in range(16)])

PLAINTEXTS = [
    (c.DIR_H2D, '{"t":"sessions","s":[{"n":1,"id":"0f3c9a1e","name":"cardputer-cli-buddy","title":"実装して","state":"idle"}]}'),
    (c.DIR_H2D, '{"t":"perm","n":1,"req":"r1","tool":"Bash","hint":"rm -rf ./build"}'),
    (c.DIR_D2H, '{"t":"perm_reply","req":"r1","decision":"allow"}'),
    (c.DIR_D2H, '{"t":"prompt","n":1,"id":"0f3c9a1e","text":"' + "x" * 100 + '"}'),
]


def hx(b: bytes) -> str:
    return b.hex()


def main():
    enc, mac = c.hkdf(KEY, NH, ND)
    host = c.Session(enc, mac, c.DIR_H2D)
    dev = c.Session(enc, mac, c.DIR_D2H)
    frames = []
    for d, pt in PLAINTEXTS:
        s = host if d == c.DIR_H2D else dev
        line = s.seal_bytes(pt.encode())
        frames.append({"dir": d, "ctr": s.tx_ctr, "pt": pt, "line": line.decode().rstrip("\n")})

    # Audio は frames の続きとして同じ device→host カウンタで seal する
    audio_pt = bytes([0x00, 0x01]) + bytes(range(256)) * 2
    audio = [{"dir": c.DIR_D2H, "ctr": dev.tx_ctr + 1, "pt_hex": audio_pt.hex(),
              "line": dev.seal_bytes(audio_pt, c.AUDIO).decode().rstrip("\n")}]

    h2d = [f for f in frames if f["dir"] == c.DIR_H2D]
    first = h2d[0]["line"]

    def flip(line: str, idx: int) -> str:
        raw = bytearray(c._b64(line.encode()))
        raw[idx] ^= 0x01
        return base64.b64encode(bytes(raw)).decode()

    old_enc, old_mac = c.hkdf(KEY, NH, ND_OLD)
    old = c.Session(old_enc, old_mac, c.DIR_H2D)
    old_line = old.seal_bytes(PLAINTEXTS[0][1].encode()).decode().rstrip("\n")

    out = {
        "_doc": "PROTOCOL.md のテストベクタ。bytes は hex。frames は順に seal した結果で、"
        "受信側はその順に open して pt と一致すること。audio_frames は frames の後に同じセッションで seal した Audio フレーム（平文は pt_hex）。reject は、frames をすべて受理したあとの"
        "受信状態（rx_ctr = 各方向の最後の ctr）で open し、すべて拒否されること。",
        "key": hx(KEY),
        "nh": hx(NH),
        "nd": hx(ND),
        "enc_key": hx(enc),
        "mac_key": hx(mac),
        "hello_host": c.hello(c.ROLE_HOST, NH).decode().rstrip("\n"),
        "hello_device": c.hello(c.ROLE_DEVICE, ND).decode().rstrip("\n"),
        "frames": frames,
        "audio_frames": audio,
        "reject_h2d": [
            {"why": "replay", "line": first},
            {"why": "tampered ciphertext", "line": flip(h2d[-1]["line"], 8)},
            {"why": "tampered tag", "line": flip(h2d[-1]["line"], -1)},
            {"why": "tampered ctr", "line": flip(h2d[-1]["line"], 6)},
            {"why": "previous session (other nd)", "line": old_line},
            {"why": "wrong direction", "line": [f for f in frames if f["dir"] == c.DIR_D2H][0]["line"]},
            {"why": "plaintext json", "line": '{"t":"perm_reply","req":"r1","decision":"allow"}'},
            {"why": "bad base64", "line": "!!!!"},
        ],
    }
    path = pathlib.Path(__file__).with_name("envelope.json")
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
