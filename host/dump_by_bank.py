"""
バンク単位でダンプする。1バンクごとにNanoをリセットして読み直す。

長時間連続で読み続けると読み取りが化けるが、1バンク(64KB)だけを起動直後に読む分には
再現性が完全（同じバンクを2回読んで差分0）。それを利用して、

    バンクごとに「2回読んで完全一致するまで繰り返す」

という形で確実なデータだけを積み上げる。

    python dump_by_bank.py --port COM12 --banks 32 --mapping hirom --out MyGame.sfc
"""

import argparse
import os
import sys
import time

import serial

BANK_SIZE = 65536


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--banks", type=int, required=True)
    p.add_argument("--mapping", choices=["hirom", "lorom"], required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--baud", type=int, default=250000)
    p.add_argument("--max-attempts", type=int, default=8,
                   help="1バンクあたり、一致するまで読み直す最大回数")
    p.add_argument("--cache", default=None, help="確定したバンクの保存先 (既定: <out>.banks)")
    return p.parse_args()


def read_bank(port, baud, bank):
    """Nanoをリセットしてバンク番号を送り、64KB受け取る。失敗したら None。"""
    try:
        ser = serial.Serial(port, baud, timeout=30)
    except Exception as e:
        print(f"    ポートを開けません: {e}", flush=True)
        return None
    try:
        # Nanoが 'R' を送ってくるまで待つ（起動＋スプラッシュ表示ぶん）
        deadline = time.time() + 20
        while time.time() < deadline:
            b = ser.read(1)
            if b == b"R":
                break
        else:
            print("    Nanoからの準備完了(R)が来ませんでした", flush=True)
            return None

        ser.write(bytes([bank]))   # 読みたいバンクを指示
        ser.flush()

        buf = bytearray()
        while len(buf) < BANK_SIZE:
            chunk = ser.read(min(4096, BANK_SIZE - len(buf)))
            if not chunk:
                print(f"    タイムアウト ({len(buf)}/{BANK_SIZE})", flush=True)
                return None
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def confirm_bank(port, baud, bank, max_attempts):
    """同じバンクを読み直し、2回連続で完全一致した内容を返す。"""
    prev = None
    for attempt in range(1, max_attempts + 1):
        data = read_bank(port, baud, bank)
        if data is None:
            continue
        if prev is not None:
            diff = sum(1 for a, b in zip(prev, data) if a != b)
            if diff == 0:
                print(f"    確定 (試行{attempt})", flush=True)
                return data
            print(f"    試行{attempt}: 前回と {diff} バイト相違、読み直します", flush=True)
        prev = data
    return None


def extract_rom(raw, mapping):
    if mapping == "hirom":
        return raw
    out = bytearray()
    for i in range(0, len(raw), BANK_SIZE):
        out += raw[i:i + BANK_SIZE // 2]
    return bytes(out)


def verify(rom, mapping):
    off = 0xFFC0 if mapping == "hirom" else 0x7FC0
    if len(rom) < off + 32:
        return False, None, None
    complement = rom[off + 28] | (rom[off + 29] << 8)
    checksum = rom[off + 30] | (rom[off + 31] << 8)
    if ((checksum + complement) & 0xFFFF) != 0xFFFF:
        return False, None, checksum
    computed = sum(rom) & 0xFFFF
    return computed == checksum, computed, checksum


def main():
    args = parse_args()
    cache = args.cache or (args.out + ".banks")
    os.makedirs(cache, exist_ok=True)

    banks = []
    for b in range(args.banks):
        path = os.path.join(cache, f"bank_{b:03d}.bin")
        if os.path.exists(path) and os.path.getsize(path) == BANK_SIZE:
            with open(path, "rb") as f:
                banks.append(f.read())
            print(f"bank {b:3d}: キャッシュ済み", flush=True)
            continue

        print(f"bank {b:3d}: 読み出し中", flush=True)
        data = confirm_bank(args.port, args.baud, b, args.max_attempts)
        if data is None:
            print(f"bank {b:3d}: 一致を得られませんでした。中断します。", flush=True)
            return 1
        with open(path, "wb") as f:
            f.write(data)
        banks.append(data)

    raw = b"".join(banks)
    rom = extract_rom(raw, args.mapping)
    ok, computed, expected = verify(rom, args.mapping)
    print(f"\n合計 {len(rom)} bytes / 計算値={hex(computed) if computed is not None else 'NA'} "
          f"期待値={hex(expected) if expected is not None else 'NA'} -> "
          f"{'一致' if ok else '不一致'}", flush=True)

    out = args.out if ok else args.out + ".unverified"
    with open(out, "wb") as f:
        f.write(rom)
    print(f"保存: {out}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
