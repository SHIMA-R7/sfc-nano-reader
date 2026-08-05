"""
RD_SETTLE / ADDR_SETTLE / PULSE を振って、既知の正解バンクと照合しながら最速設定を探す。

各条件で **2回読んで両方とも正解と完全一致** した場合のみ合格とする。
（1回だけの一致は当てにならない。実際PULSE=60は単発では完全一致したのに、
  繰り返すと数千バイト化けた）

    python timing_tune.py --port COM12 --cache SuperMarioCollection.sfc.banks --bank 21
"""

import argparse
import io
import os
import re
import subprocess
import sys
import time

import serial

BANK_SIZE = 65536

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKETCH = os.path.join(ROOT, "nano2_master")
INO = os.path.join(SKETCH, "nano2_master.ino")
FQBN = "arduino:avr:nano:cpu=atmega328old"
CLI = r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"

# (RD_SETTLE_US, ADDR_SETTLE_US, PULSE_US)
CASES = [
    (20, 20, 5),
    (10, 10, 3),
    (5, 5, 3),
    (3, 3, 2),
    (2, 2, 2),
]


def set_timing(rd, addr, pulse):
    s = io.open(INO, encoding="utf-8").read()
    s = re.sub(r"const uint16_t RD_SETTLE_US = \d+;",
               f"const uint16_t RD_SETTLE_US = {rd};", s)
    s = re.sub(r"const uint16_t ADDR_SETTLE_US = \d+;",
               f"const uint16_t ADDR_SETTLE_US = {addr};", s)
    s = re.sub(r"const uint16_t PULSE_US = \d+;",
               f"const uint16_t PULSE_US = {pulse};", s)
    io.open(INO, "w", encoding="utf-8").write(s)


def build_and_flash(port):
    for args in (["compile", "--fqbn", FQBN, SKETCH],
                 ["upload", "-p", port, "--fqbn", FQBN, SKETCH]):
        for attempt in range(2):
            r = subprocess.run([CLI] + args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0:
                break
            time.sleep(2)
        else:
            return False
    return True


def read_bank(port, baud, bank):
    try:
        ser = serial.Serial(port, baud, timeout=30)
    except Exception:
        return None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ser.read(1) == b"R":
                break
        else:
            return None
        ser.write(bytes([bank]))
        ser.flush()
        buf = bytearray()
        t0 = time.time()
        while len(buf) < BANK_SIZE:
            chunk = ser.read(min(4096, BANK_SIZE - len(buf)))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf), time.time() - t0
    finally:
        ser.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--cache", required=True)
    ap.add_argument("--bank", type=int, default=21)
    ap.add_argument("--baud", type=int, default=1000000)
    args = ap.parse_args()

    with open(os.path.join(args.cache, f"bank_{args.bank:03d}.bin"), "rb") as f:
        good = f.read()

    results = []
    for rd, addr, pulse in CASES:
        print(f"=== RD={rd} ADDR={addr} PULSE={pulse} ===", flush=True)
        set_timing(rd, addr, pulse)
        if not build_and_flash(args.port):
            print("    書き込み失敗、スキップ", flush=True)
            continue

        diffs, times = [], []
        for trial in range(2):
            got = read_bank(args.port, args.baud, args.bank)
            if got is None:
                print(f"    {trial+1}回目: 読み出し失敗", flush=True)
                diffs.append(-1)
                break
            data, el = got
            d = sum(1 for a, b in zip(good, data) if a != b)
            diffs.append(d)
            times.append(el)
            print(f"    {trial+1}回目: {'完全一致' if d == 0 else f'{d} バイト相違'}"
                  f" / {el:.2f}秒", flush=True)

        ok = len(diffs) == 2 and all(d == 0 for d in diffs)
        per_byte = (sum(times) / len(times) / BANK_SIZE * 1e6) if times else 0
        results.append((rd, addr, pulse, ok, per_byte))

    print("\n=== まとめ (2回とも完全一致した設定のみ合格) ===", flush=True)
    for rd, addr, pulse, ok, per_byte in results:
        est = per_byte * BANK_SIZE * 64 / 1e6
        mark = "合格" if ok else "不合格"
        print(f"  RD={rd:2d} ADDR={addr:2d} PULSE={pulse:2d} -> {mark} "
              f"({per_byte:.0f}us/byte, 2MB約{est:.0f}秒)", flush=True)

    passed = [r for r in results if r[3]]
    if passed:
        best = min(passed, key=lambda r: r[4])
        print(f"\n最速の合格設定: RD={best[0]} ADDR={best[1]} PULSE={best[2]} "
              f"({best[4]:.0f}us/byte)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
