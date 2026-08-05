"""
同じバンクを繰り返し読み、誤りが「累積的に悪化するのか」を調べる。

毎回リセットして読み、既知の正解バンクと照合する。経過時間と誤り数を並べて出すので、
  - 序盤きれい / 後半汚い  -> 累積性。休憩を挟めば回避できる可能性がある
  - 最初からばらつく        -> 累積ではない
が判別できる。

    python degradation_test.py --port COM12 --cache SuperMarioCollection.sfc.banks --bank 21 --reads 10

--rest を付けると毎回の読み出し前に指定秒だけ休む（休憩の効果を見る用）。
"""

import argparse
import os
import sys
import time

import serial

BANK_SIZE = 65536


def read_bank(port, baud, bank):
    try:
        ser = serial.Serial(port, baud, timeout=30)
    except Exception as e:
        print(f"    ポートを開けません: {e}", flush=True)
        return None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ser.read(1) == b"R":
                break
        else:
            print("    準備完了(R)が来ませんでした", flush=True)
            return None

        ser.write(bytes([bank]))
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--bank", type=int, default=21)
    ap.add_argument("--reads", type=int, default=10)
    ap.add_argument("--rest", type=float, default=0.0, help="各読み出し前に休む秒数")
    ap.add_argument("--baud", type=int, default=1000000)
    args = ap.parse_args()

    with open(os.path.join(args.cache, f"bank_{args.bank:03d}.bin"), "rb") as f:
        good = f.read()

    t0 = time.time()
    rows = []
    for i in range(1, args.reads + 1):
        if args.rest:
            time.sleep(args.rest)
        data = read_bank(args.port, args.baud, args.bank)
        if data is None:
            print(f"{i:3d}回目: 読み出し失敗", flush=True)
            continue
        diff = sum(1 for a, b in zip(good, data) if a != b)
        el = time.time() - t0
        rows.append((i, el, diff))
        mark = "完全一致" if diff == 0 else f"{diff:6d} バイト相違"
        print(f"{i:3d}回目 (経過{el:6.1f}秒): {mark}", flush=True)

    print("\n=== まとめ ===", flush=True)
    for i, el, diff in rows:
        bar = "#" * min(60, diff // 100)
        print(f"  {i:3d} | {el:6.1f}s | {diff:6d} {bar}", flush=True)

    clean = sum(1 for _, _, d in rows if d == 0)
    print(f"\n完全一致: {clean}/{len(rows)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
