"""
libretro の .rdb (RetroArch データベース) を読むための最小実装。

RetroArch に同梱されている
    database/rdb/Nintendo - Super Nintendo Entertainment System.rdb
を解析して、タイトル名から ROM サイズ(バイト)を引けるようにする。

フォーマット:
    先頭16バイト  "RARCHDB\0" + 8バイトのメタデータオフセット(BE)
    以降          MessagePack の map が連続で並ぶ

外部ライブラリに依存しないよう、必要な範囲だけの MessagePack デコーダを内蔵している。
"""

import os
import struct

MAGIC = b"RARCHDB\x00"


class _Reader:
    def __init__(self, data, pos=0):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def take(self, n):
        v = self.d[self.p:self.p + n]
        self.p += n
        return v

    def num(self, fmt, n):
        v = struct.unpack_from(fmt, self.d, self.p)[0]
        self.p += n
        return v


def _decode(r):
    """MessagePack の値を1つ読む。未対応の型に当たったら ValueError。"""
    b = r.u8()

    if b <= 0x7F:                      # positive fixint
        return b
    if b >= 0xE0:                      # negative fixint
        return b - 0x100
    if 0x80 <= b <= 0x8F:              # fixmap
        return _decode_map(r, b & 0x0F)
    if 0x90 <= b <= 0x9F:              # fixarray
        return [_decode(r) for _ in range(b & 0x0F)]
    if 0xA0 <= b <= 0xBF:              # fixstr
        return r.take(b & 0x1F)

    if b == 0xC0:
        return None
    if b == 0xC2:
        return False
    if b == 0xC3:
        return True
    if b == 0xC4:
        return r.take(r.u8())                       # bin8
    if b == 0xC5:
        return r.take(r.num(">H", 2))               # bin16
    if b == 0xC6:
        return r.take(r.num(">I", 4))               # bin32
    if b == 0xCA:
        return r.num(">f", 4)
    if b == 0xCB:
        return r.num(">d", 8)
    if b == 0xCC:
        return r.u8()
    if b == 0xCD:
        return r.num(">H", 2)
    if b == 0xCE:
        return r.num(">I", 4)
    if b == 0xCF:
        return r.num(">Q", 8)
    if b == 0xD0:
        return r.num(">b", 1)
    if b == 0xD1:
        return r.num(">h", 2)
    if b == 0xD2:
        return r.num(">i", 4)
    if b == 0xD3:
        return r.num(">q", 8)
    if b == 0xD9:
        return r.take(r.u8())                       # str8
    if b == 0xDA:
        return r.take(r.num(">H", 2))               # str16
    if b == 0xDB:
        return r.take(r.num(">I", 4))               # str32
    if b == 0xDC:
        n = r.num(">H", 2)
        return [_decode(r) for _ in range(n)]
    if b == 0xDD:
        n = r.num(">I", 4)
        return [_decode(r) for _ in range(n)]
    if b == 0xDE:
        return _decode_map(r, r.num(">H", 2))
    if b == 0xDF:
        return _decode_map(r, r.num(">I", 4))

    raise ValueError(f"未対応の MessagePack タイプ: 0x{b:02x}")


def _decode_map(r, count):
    out = {}
    for _ in range(count):
        k = _decode(r)
        v = _decode(r)
        if isinstance(k, bytes):
            k = k.decode("utf-8", "replace")
        out[k] = v
    return out


def _as_text(v):
    if isinstance(v, bytes):
        return v.decode("utf-8", "replace")
    return v


def load_entries(path):
    """.rdb を読み、{name, size, crc, serial} の辞書リストを返す。"""
    with open(path, "rb") as f:
        data = f.read()

    if not data.startswith(MAGIC):
        raise ValueError("RARCHDB ヘッダが見つかりません。")

    # 先頭16バイトのあとにメタデータオフセットが入っている。
    # そこから先はインデックス領域なので、エントリ本体はその手前まで。
    meta_offset = struct.unpack_from(">Q", data, 8)[0]
    end = meta_offset if 0 < meta_offset <= len(data) else len(data)

    r = _Reader(data, 16)
    entries = []
    while r.p < end:
        try:
            item = _decode(r)
        except (ValueError, IndexError, struct.error):
            break
        if not isinstance(item, dict):
            continue
        name = _as_text(item.get("name"))
        if not name:
            continue
        entries.append({
            "name": name,
            "size": item.get("size"),
            "crc": item.get("crc"),
            "serial": _as_text(item.get("serial")),
        })
    return entries


def default_rdb_paths():
    """よくある場所の .rdb を候補として返す。"""
    rel = os.path.join("database", "rdb", "Nintendo - Super Nintendo Entertainment System.rdb")
    candidates = [
        os.path.join(r"C:\Users", os.environ.get("USERNAME", ""), "Downloads", "RetroArch",
                     "RetroArch-Win64", rel),
        os.path.join(os.path.expandvars(r"%APPDATA%"), "RetroArch", rel),
        os.path.join(os.path.expandvars(r"%LOCALAPPDATA%"), "RetroArch", rel),
    ]
    return [p for p in candidates if os.path.exists(p)]


def search(entries, query, limit=50):
    """部分一致・大文字小文字無視でタイトル検索する。"""
    q = query.lower().strip()
    if not q:
        return []
    hits = [e for e in entries if q in e["name"].lower()]
    hits.sort(key=lambda e: (not e["name"].lower().startswith(q), e["name"]))
    return hits[:limit]


if __name__ == "__main__":
    import sys
    paths = default_rdb_paths()
    if not paths:
        print("SNES の .rdb が見つかりません。")
        sys.exit(1)
    ents = load_entries(paths[0])
    print(f"{len(ents)} 件を読み込みました: {paths[0]}")
    q = sys.argv[1] if len(sys.argv) > 1 else "mario"
    for e in search(ents, q, 15):
        kb = e["size"] // 1024 if e["size"] else "?"
        print(f"  {e['name']} : {e['size']} bytes ({kb} KB)")
