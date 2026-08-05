"""
STROBEパルス幅を変えながら、既知の正解バンクと照合して最小の安全値を探す。

Nano-1/Nano-3 のポーリング周期よりパルスが短いとSTROBEを取りこぼし、
アドレスが進まずに同じ値を読み続ける。どこまで詰められるかを実測で決める。

Nano-2だけ書き換えれば済むので、接続しなおしは不要。

    python pulse_sweep.py --port COM12 --cache SuperMarioCollection.sfc.banks --bank 21
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


def set_pulse(us):
    s = io.open(INO, encoding="utf-8").read()
    s = re.sub(r"const uint8_t PULSE_US = \d+;", f"const uint8_t PULSE_US = {us};", s)
    io.open(INO, "w", encoding="utf-8").write(s)


def build_and_flash(port):
    for args in (["compile", "--fqbn", FQBN, SKETCH],
                 ["upload", "-p", port, "--fqbn", FQBN, SKETCH]):
        r = subprocess.run([CLI] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("    書き込み失敗、リトライします", flush=True)
            time.sleep(2)
            r = subprocess.run([CLI] + args, capture_output=True, text=True,
                               encoding="utf-8", errors="replace")
            if r.returncode != 0:
                return False
    return True


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
    ap.add_argument("--values", default="15,25,40,60,100")
    args = ap.parse_args()

    with open(os.path.join(args.cache, f"bank_{args.bank:03d}.bin"), "rb") as f:
        good = f.read()

    results = []
    for us in [int(x) for x in args.values.split(",")]:
        print(f"=== PULSE_US = {us} ===", flush=True)
        set_pulse(us)
        if not build_and_flash(args.port):
            print("    スキップ", flush=True)
            continue

        got = read_bank(args.port, args.baud, args.bank)
        if got is None:
            print("    読み出し失敗", flush=True)
            continue
        data, elapsed = got
        diff = sum(1 for a, b in zip(good, data) if a != b)
        kbs = BANK_SIZE / elapsed / 1024
        mark = "完全一致" if diff == 0 else f"{diff} バイト相違"
        print(f"    {mark} / {elapsed:.2f}秒 ({kbs:.1f} KB/s)", flush=True)
        results.append((us, diff, elapsed))

    print("\n=== まとめ ===", flush=True)
    for us, diff, elapsed in results:
        est = elapsed * 32  # 2MB(=64バンク x 32KB相当)の目安
        print(f"  PULSE={us:3d}us -> 差分 {diff:6d} / 1バンク {elapsed:.2f}秒 "
              f"(2MB換算 約{est:.0f}秒)", flush=True)

    ok = [r for r in results if r[1] == 0]
    if ok:
        best = min(ok, key=lambda r: r[0])
        print(f"\n完全一致した最小値: PULSE_US = {best[0]}", flush=True)
    else:
        print("\n完全一致した設定がありませんでした。", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
