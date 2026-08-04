"""
Nano-2 のUSBシリアルから生バイトを受信し、ファイルに保存する。

使い方:
    pip install pyserial
    python receive.py COM5 4194304 dump.bin

引数:
    COM5       Nano-2のシリアルポート（デバイスマネージャ等で確認）
    4194304    受信予定バイト数。nano2_master.ino の NUM_BANKS * 65536 と一致させる
    dump.bin   出力ファイル名
"""

import sys
import time
import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM5"
BAUD = 250000
SIZE_BYTES = int(sys.argv[2]) if len(sys.argv) > 2 else 64 * 65536
OUT_FILE = sys.argv[3] if len(sys.argv) > 3 else "dump.bin"


def main():
    ser = serial.Serial(PORT, BAUD, timeout=25)  # OLEDスプラッシュ演出込みで初回データまで約14秒かかるため余裕を持たせる
    time.sleep(2)  # Nano自動リセット(DTR)後、起動待ちのnano2側delay(3000)に間に合わせる
    ser.reset_input_buffer()  # 前回セッションの残留バイトを破棄してから読み始める

    received = 0
    start = time.time()
    with open(OUT_FILE, "wb") as f:
        while received < SIZE_BYTES:
            remaining = SIZE_BYTES - received
            chunk = ser.read(min(4096, remaining))
            if not chunk:
                print(f"\nタイムアウト: {received}/{SIZE_BYTES} バイトで停止")
                break
            f.write(chunk)
            received += len(chunk)
            pct = received / SIZE_BYTES * 100
            print(f"\r{received}/{SIZE_BYTES} bytes ({pct:.1f}%)", end="")

    elapsed = time.time() - start
    if elapsed > 0:
        print(f"\n完了: {received} bytes, {elapsed:.1f}s, {received/elapsed/1024:.1f} KB/s")
    ser.close()


if __name__ == "__main__":
    main()
