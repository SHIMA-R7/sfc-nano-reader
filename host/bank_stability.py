"""
同じバンクを何度も読み、値が本当に安定しているかを調べる。

「2回読んで一致」でも、決定的に同じ誤り方をしていれば通ってしまう。
より多く読んで全部一致するか、あるいは何通りの値が出るかを見る。

    python bank_stability.py --port COM12 --bank 21 --reads 5
"""

import argparse
import hashlib
import sys
import time
from collections import Counter

import serial

BANK_SIZE = 65536


def read_bank(port, baud, bank):
    try:
        ser = serial.Serial(port, baud, timeout=30)
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
    ap.add_argument("--bank", type=int, required=True)
    ap.add_argument("--reads", type=int, default=5)
    ap.add_argument("--baud", type=int, default=250000)
    args = ap.parse_args()

    reads = []
    for i in range(args.reads):
        d = read_bank(args.port, args.baud, args.bank)
        if d is None:
            continue
        h = hashlib.sha1(d).hexdigest()[:12]
        print(f"  読み {i+1}: sha1={h}", flush=True)
        reads.append(d)

    if not reads:
        print("読み出せませんでした。", flush=True)
        return 1

    c = Counter(reads)
    print(f"\nbank {args.bank}: {len(reads)}回中 {len(c)} 通りの値", flush=True)
    for i, (val, cnt) in enumerate(c.most_common()):
        print(f"  値{i+1}: {cnt}回  sha1={hashlib.sha1(val).hexdigest()[:12]}", flush=True)

    if len(c) > 1:
        vals = list(c)
        diff = sum(1 for a, b in zip(vals[0], vals[1]) if a != b)
        print(f"  上位2つの差分: {diff} バイト", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
