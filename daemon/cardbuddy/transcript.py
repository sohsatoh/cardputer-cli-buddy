"""Claude Code の transcript（<CLAUDE_CONFIG_DIR>/projects/<cwd>/<sid>.jsonl）を Web 向けに読む。

transcript は大きくなるので、行の先頭オフセットの索引を sid ごとに持ち、増えた分だけ読み足す。
"""

import glob
import json
import os
import re
import threading
from pathlib import Path

DEFAULT_PATTERN = "~/.claude*/projects/*/{sid}.jsonl"
ENTRY_MAX = 4000
LIMIT_DEFAULT, LIMIT_MAX = 200, 1000
# glob の特殊文字やパスの区切りを含む sid で、任意のファイルを読ませない
SAFE_SID = re.compile(r"[0-9A-Za-z_-]{1,128}")
SUMMARY_KEYS = ("command", "file_path", "notebook_path", "pattern", "url", "query", "description", "prompt")


class _Index:
    def __init__(self, path: Path):
        self.path = path
        self.offsets: list[int] = []  # 完結した各行の先頭
        self.scanned = 0  # ここまでのバイトは改行で区切り終えている

    def refresh(self):
        size = self.path.stat().st_size
        if size < self.scanned:
            self.offsets, self.scanned = [], 0
        if size == self.scanned:
            return
        line_start = pos = self.scanned
        with self.path.open("rb") as f:
            f.seek(pos)
            for chunk in iter(lambda: f.read(1 << 20), b""):
                i = 0
                while (nl := chunk.find(b"\n", i)) >= 0:
                    self.offsets.append(line_start)
                    line_start = pos + nl + 1
                    i = nl + 1
                pos += len(chunk)
        # 書きかけの最後の行は、改行が来るまで索引に入れない
        self.scanned = line_start

    def lines(self, start: int, end: int) -> list[bytes]:
        if start >= end:
            return []
        stop = self.offsets[end] if end < len(self.offsets) else self.scanned
        with self.path.open("rb") as f:
            f.seek(self.offsets[start])
            data = f.read(stop - self.offsets[start])
        return data.split(b"\n")[: end - start]


CACHE: dict[str, _Index] = {}
# Web はスレッドで read を呼ぶので、sid ごとの索引の更新を 1 本ずつにする
_LOCK = threading.Lock()


def find(sid: str) -> Path | None:
    pattern = os.path.expanduser(os.environ.get("CARDBUDDY_TRANSCRIPTS", DEFAULT_PATTERN))
    found = {}
    for p in map(Path, glob.glob(pattern.replace("{sid}", sid))):
        # ファイル自体の symlink と、projects の外を指すディレクトリの symlink を拒む（projects 自体の symlink は許す）
        if p.is_symlink() or not p.is_file():
            continue
        real, root = p.resolve(), p.parent.parent.resolve()
        if real.is_relative_to(root):
            found[real] = real.stat().st_mtime
    return max(found, key=found.get) if found else None


def read(table, sid: str, before: int | None = None, after: int | None = None,
         limit: int = LIMIT_DEFAULT) -> dict | None:
    if sid not in table.sessions or not SAFE_SID.fullmatch(sid):
        return None
    path = find(sid)
    if path is None:
        return None
    with _LOCK:
        idx = CACHE.get(sid)
        if idx is None or idx.path != path:
            idx = CACHE[sid] = _Index(path)
        idx.refresh()
        total = len(idx.offsets)
    limit = max(1, min(limit, LIMIT_MAX))
    if after is not None:
        start = min(max(after, 0), total)
        end = min(start + limit, total)
    else:
        end = total if before is None else min(max(before, 0), total)
        start = max(end - limit, 0)
    items = []
    with _LOCK:
        raws = idx.lines(start, end)
    for n, raw in enumerate(raws, start):
        items += parse_line(n, raw)
    return {"start": start, "end": end, "total": total, "items": items}


def _cut(text: str) -> str:
    return text if len(text) <= ENTRY_MAX else f"{text[:ENTRY_MAX]}…（{len(text) - ENTRY_MAX} 文字省略）"


def _summary(inp) -> str:
    if isinstance(inp, dict):
        for k in SUMMARY_KEYS:
            if isinstance(inp.get(k), str):
                return inp[k].split("\n", 1)[0][:200]
    return json.dumps(inp, ensure_ascii=False)[:200]


def _result_text(content) -> str:
    if isinstance(content, str):
        return content
    parts = []
    for b in content if isinstance(content, list) else []:
        if isinstance(b, dict):
            parts.append(b["text"] if b.get("type") == "text" and isinstance(b.get("text"), str) else "[画像]"
                         if b.get("type") == "image" else f"[{b.get('type')}]")
    return "\n".join(parts)


def parse_line(n: int, raw: bytes) -> list[dict]:
    try:
        e = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(e, dict) or e.get("type") not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
        return []
    msg = e.get("message")
    content = msg.get("content") if isinstance(msg, dict) else None
    if e["type"] == "user" and isinstance(content, str):
        return [{"line": n, "kind": "user", "text": _cut(content)}]
    out = []
    for b in content if isinstance(content, list) else []:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text" and isinstance(b.get("text"), str):
            out.append({"line": n, "kind": e["type"], "text": _cut(b["text"])})
        elif t in ("tool_use", "server_tool_use"):
            out.append({"line": n, "kind": "tool_use", "name": str(b.get("name", "?")),
                        "summary": _summary(b.get("input")),
                        "text": _cut(json.dumps(b.get("input"), ensure_ascii=False, indent=1))})
        elif t == "tool_result":
            out.append({"line": n, "kind": "tool_result", "text": _cut(_result_text(b.get("content"))),
                        "is_error": b.get("is_error") is True})
    return out
