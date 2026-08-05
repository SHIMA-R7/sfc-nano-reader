"""
読み出しタイミングを変えながら同じバンクを2回ずつ読み、再現性（誤り率）を測る。

nano2_master.ino の RD_SETTLE_US / ADDR_SETTLE_US を書き換えて再ビルド・書き込みし、
同じバンクを2回読んで一致率を見る。誤りがタイミング起因なら、待ちを長くすると
一致率が上がるはず。変わらなければタイミングは原因ではない。

    python timing_sweep.py COM12
"""

import io
import os
import re
import subprocess
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM12"
BAUD = 250000
BANK_SIZE = 65536

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKETCH = os.path.join(ROOT, "nano2_master")
INO = os.path.join(SKETCH, "nano2_master.ino")
FQBN = "arduino:avr:nano:cpu=atmega328old"
CLI = r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"

# (RD_SETTLE_US, ADDR_SETTLE_US)
CASES = [
    (20, 20),
    (100, 100),   # 現行設定
    (400, 400),
    (1000, 1000),
]


def set_timing(rd_us, addr_us):
    s = io.open(INO, encoding="utf-8").read()
    s = re.sub(r"const uint16_t RD_SETTLE_US = \d+;",
               f"const uint16_t RD_SETTLE_US = {rd_us};", s)
    s = re.sub(r"const uint16_t ADDR_SETTLE_US = \d+;",
               f"const uint16_t ADDR_SETTLE_US = {addr_us};", s)
    io.open(INO, "w", encoding="utf-8").write(s)


def build_and_flash():
    for args in (["compile", "--fqbn", FQBN, SKETCH],
                 ["upload", "-p", PORT, "--fqbn", FQBN, SKETCH]):
        r = subprocess.run([CLI] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            print("  ビルド/書き込み失敗:", (r.stderr or r.stdout)[-400:], flush=True)
            return False
    return True


def read_once(total):
    ser = serial.Serial(PORT, BAUD, timeout=40)
    try:
        time.sleep(2)
        ser.reset_input_buffer()
        buf = bytearray()
        while len(buf) < total:
            chunk = ser.read(min(4096, total - len(buf)))
            if not chunk:
                return None
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def main():
    print(f"ポート {PORT} / 1バンク(64KB)を各条件で2回読んで比較します。\n", flush=True)
    results = []
    for rd_us, addr_us in CASES:
        print(f"=== RD_SETTLE={rd_us}us ADDR_SETTLE={addr_us}us ===", flush=True)
        set_timing(rd_us, addr_us)
        if not build_and_flash():
            continue

        a = read_once(BANK_SIZE)
        if a is None:
            print("  1回目 タイムアウト", flush=True)
            continue
        b = read_once(BANK_SIZE)
        if b is None:
            print("  2回目 タイムアウト", flush=True)
            continue

        diff = sum(1 for x, y in zip(a, b) if x != y)
        pct = diff / BANK_SIZE * 100
        print(f"  2回の差分: {diff} / {BANK_SIZE} バイト ({pct:.3f}%)", flush=True)
        results.append((rd_us, addr_us, diff))

    print("\n=== まとめ ===", flush=True)
    for rd_us, addr_us, diff in results:
        print(f"  RD={rd_us:5d}us ADDR={addr_us:5d}us -> 差分 {diff}", flush=True)

    # 現行設定に戻す
    set_timing(100, 100)
    build_and_flash()
    print("\n現行設定(100/100)に戻しました。", flush=True)


if __name__ == "__main__":
    main()
