"""
チェックサムが一致するまでダンプを繰り返し、バイト単位の多数決でマージする汎用ツール。

使い方:
    python dump_until_valid.py --port COM12 --banks 32 --mapping hirom --out MyGame.sfc

--mapping hirom : 1バンク64KBをそのまま使う（HiROM）
--mapping lorom : 1バンク64KBのうち前半32KBだけ採用（LoROMはA15ミラーで後半が同一）

途中経過は work/ 以下に raw_NN.bin として保存され、再実行時に自動で読み込まれる。
"""

import argparse
import glob
import os
import sys
import time
from collections import Counter

import serial

BANK_SIZE = 65536


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--banks", type=int, required=True)
    p.add_argument("--mapping", choices=["hirom", "lorom"], required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--baud", type=int, default=250000)
    p.add_argument("--max-rounds", type=int, default=60)
    p.add_argument("--work", default=None, help="中間ファイル保存先 (既定: <out>.work)")
    return p.parse_args()


def receive_once(port, baud, total_bytes, path):
    """1回分の生ダンプを受信してファイルに保存。完走したら True。"""
    try:
        ser = serial.Serial(port, baud, timeout=25)
    except Exception as e:
        print(f"  ポートオープン失敗: {e}", flush=True)
        return False

    try:
        time.sleep(2)              # Nano自動リセット後の起動待ち
        ser.reset_input_buffer()   # 前セッションの残留バイトを破棄

        buf = bytearray()
        start = time.time()
        while len(buf) < total_bytes:
            chunk = ser.read(min(4096, total_bytes - len(buf)))
            if not chunk:
                print(f"  タイムアウト: {len(buf)}/{total_bytes} バイトで停止", flush=True)
                return False
            buf += chunk
        elapsed = time.time() - start
    finally:
        ser.close()

    with open(path, "wb") as f:
        f.write(buf)
    print(f"  受信完了 {len(buf)} bytes ({elapsed:.1f}s)", flush=True)
    return True


def extract_rom(raw, mapping):
    """生ダンプからROM本体を取り出す。"""
    if mapping == "hirom":
        return raw
    out = bytearray()
    for i in range(0, len(raw), BANK_SIZE):
        out += raw[i:i + BANK_SIZE // 2]
    return bytes(out)


def header_offset(rom, mapping):
    return 0xFFC0 if mapping == "hirom" else 0x7FC0


def verify(rom, mapping):
    """(一致したか, 計算値, ヘッダ値) を返す。"""
    off = header_offset(rom, mapping)
    if len(rom) < off + 32:
        return False, None, None
    complement = rom[off + 28] | (rom[off + 29] << 8)
    checksum = rom[off + 30] | (rom[off + 31] << 8)
    if ((checksum + complement) & 0xFFFF) != 0xFFFF:
        return False, None, checksum
    computed = sum(rom) & 0xFFFF
    return computed == checksum, computed, checksum


def majority_merge(samples):
    n = len(samples[0])
    merged = bytearray(n)
    disputed = 0
    for i in range(n):
        col = [s[i] for s in samples]
        best, cnt = Counter(col).most_common(1)[0]
        merged[i] = best
        if cnt < len(samples):
            disputed += 1
    return bytes(merged), disputed


def main():
    args = parse_args()
    work = args.work or (args.out + ".work")
    os.makedirs(work, exist_ok=True)

    total_bytes = args.banks * BANK_SIZE

    raws = []
    for path in sorted(glob.glob(os.path.join(work, "raw_*.bin"))):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) == total_bytes:
            raws.append(data)
        else:
            print(f"サイズ不一致のため無視: {path}", flush=True)
    if raws:
        print(f"既存の中間ファイルを {len(raws)} 個読み込みました。", flush=True)

    # 1個でもあれば、まず現状でマージ判定してみる
    for round_no in range(1, args.max_rounds + 1):
        if raws:
            roms = [extract_rom(r, args.mapping) for r in raws]
            merged, disputed = majority_merge(roms) if len(roms) > 1 else (roms[0], 0)
            ok, computed, expected = verify(merged, args.mapping)
            print(f"[判定] サンプル{len(raws)}個 / 不一致バイト{disputed} / "
                  f"計算値={hex(computed) if computed is not None else 'NA'} "
                  f"期待値={hex(expected) if expected is not None else 'NA'} -> "
                  f"{'一致' if ok else '不一致'}", flush=True)
            if ok:
                with open(args.out, "wb") as f:
                    f.write(merged)
                print(f"成功: {args.out} ({len(merged)} bytes) を保存しました。", flush=True)
                return 0

        idx = len(raws) + 1
        print(f"=== ダンプ {idx} 回目 ===", flush=True)
        path = os.path.join(work, f"raw_{idx:02d}.bin")
        if receive_once(args.port, args.baud, total_bytes, path):
            with open(path, "rb") as f:
                raws.append(f.read())
        else:
            if os.path.exists(path):
                os.remove(path)
            time.sleep(3)

    print(f"上限 {args.max_rounds} 回に到達しました。ベストエフォート版を保存します。", flush=True)
    roms = [extract_rom(r, args.mapping) for r in raws]
    merged, _ = majority_merge(roms)
    with open(args.out + ".besteffort", "wb") as f:
        f.write(merged)
    return 1


if __name__ == "__main__":
    sys.exit(main())
