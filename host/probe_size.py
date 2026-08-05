"""
ROMの実サイズを実測する。

アドレス上位ビットがROMに繋がっていなければ、その先はミラーになる。
bank N を読んで bank 0 と一致すればそこで折り返している＝ROMはNバンク分、
一致しなければまだROMが続いている。

    python probe_size.py --port COM12 --cache MyGame.sfc.banks --probe 16,32,48
"""

import argparse
import hashlib
import os
import sys
import time

import serial

BANK_SIZE = 65536


def read_bank(port, baud, bank):
    try:
        ser = serial.Serial(port, baud, timeout=60)
    except Exception as e:
        print(f"  ポートを開けません: {e}", flush=True)
        return None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ser.read(1) == b"R":
                break
        else:
            print("  準備完了(R)が来ませんでした", flush=True)
            return None

        ser.write(bytes([bank]))
        ser.flush()

        buf = bytearray()
        while len(buf) < BANK_SIZE:
            chunk = ser.read(min(4096, BANK_SIZE - len(buf)))
            if not chunk:
                print(f"  タイムアウト ({len(buf)}/{BANK_SIZE})", flush=True)
                return None
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--probe", default="16,32,48")
    ap.add_argument("--baud", type=int, default=250000)
    args = ap.parse_args()

    ref_path = os.path.join(args.cache, "bank_000.bin")
    with open(ref_path, "rb") as f:
        bank0 = f.read()
    print(f"基準 bank0 sha1={hashlib.sha1(bank0).hexdigest()[:12]}\n", flush=True)

    for b in [int(x) for x in args.probe.split(",")]:
        data = read_bank(args.port, args.baud, b)
        if data is None:
            print(f"bank {b:3d}: 読み出し失敗", flush=True)
            continue
        same = sum(1 for x, y in zip(bank0, data) if x == y)
        h = hashlib.sha1(data).hexdigest()[:12]
        if same == BANK_SIZE:
            verdict = "bank0と完全一致 → ここで折り返している（ROMはこのバンク未満）"
        elif all(v == 0xFF for v in data[:256]):
            verdict = "全部0xFF → ROMなし（オープンバス）"
        elif all(v == 0x00 for v in data[:256]):
            verdict = "全部0x00 → ROMなし"
        else:
            verdict = f"別内容（bank0との一致 {same}/{BANK_SIZE}）→ ROMがまだ続いている"
        print(f"bank {b:3d}: sha1={h}  {verdict}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
