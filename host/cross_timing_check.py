"""
別のタイミングで読み直して、キャッシュ済みバンクと一致するか確かめる。

同じタイミングで何度読んでも同じ値が出る（＝安定）ことと、その値が正しいことは別。
決定的に同じ誤り方をしていれば、何度読んでも一致してしまう。
まったく違うタイミングで読んで一致すれば、正しい可能性がぐっと高まる。

    python cross_timing_check.py --port COM12 --cache SuperMarioCollection.sfc.banks --banks 0,1,3,21
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
    ap.add_argument("--banks", required=True, help="カンマ区切りのバンク番号")
    ap.add_argument("--baud", type=int, default=250000)
    args = ap.parse_args()

    targets = [int(x) for x in args.banks.split(",")]
    for b in targets:
        path = os.path.join(args.cache, f"bank_{b:03d}.bin")
        if not os.path.exists(path):
            print(f"bank {b:3d}: キャッシュなし、スキップ", flush=True)
            continue
        with open(path, "rb") as f:
            cached = f.read()

        data = read_bank(args.port, args.baud, b)
        if data is None:
            print(f"bank {b:3d}: 読み出し失敗", flush=True)
            continue

        diff = sum(1 for x, y in zip(cached, data) if x != y)
        mark = "一致" if diff == 0 else f"{diff} バイト相違"
        print(f"bank {b:3d}: {mark}  (cached={hashlib.sha1(cached).hexdigest()[:12]} "
              f"new={hashlib.sha1(data).hexdigest()[:12]})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
