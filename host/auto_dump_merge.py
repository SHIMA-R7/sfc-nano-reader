"""
Nano-2から複数回ダンプを取得し、バイトごとの多数決でマージして
チェックサムが正しくなる完成データを作る。

- 1回の生ダンプは2,097,152バイト(32バンク x 64KB、前半32KBが実データ・後半はミラー)
- 各回、ミラーを除いた1,048,576バイト(1MB)のユニークデータを保存
- 目標回数(TARGET_ROUNDS)集まったら多数決マージしてチェックサム検証
- チェックサムが合わなければ、目標回数を伸ばして追加ダンプを続行(MAX_ROUNDSまで)
"""

import os
import sys
import time
from collections import Counter

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM13"
BAUD = 250000
BANK_SIZE = 65536
UNIQUE_HALF = 32768
NUM_BANKS = 32
ROUND_BYTES = NUM_BANKS * BANK_SIZE  # 2,097,152

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dumps")
os.makedirs(OUT_DIR, exist_ok=True)

TARGET_ROUNDS = 20
MAX_ROUNDS = 40
EXTEND_STEP = 5


def run_one_dump(round_idx):
    for attempt in range(3):
        try:
            ser = serial.Serial(PORT, BAUD, timeout=25)  # OLEDスプラッシュ演出込みで起動〜初回データまで約14秒かかるため余裕を持たせる
            break
        except Exception as e:
            print(f"[round {round_idx}] ポートオープン失敗 (試行{attempt+1}/3): {e}", flush=True)
            time.sleep(3)
    else:
        return None, False

    time.sleep(2)  # Nano自動リセット後の起動待ち
    ser.reset_input_buffer()

    buf = bytearray()
    start = time.time()
    while len(buf) < ROUND_BYTES:
        remaining = ROUND_BYTES - len(buf)
        chunk = ser.read(min(4096, remaining))
        if not chunk:
            print(f"[round {round_idx}] タイムアウト: {len(buf)}/{ROUND_BYTES} バイトで停止", flush=True)
            break
        buf += chunk
    ser.close()
    elapsed = time.time() - start

    raw_path = os.path.join(OUT_DIR, f"raw_{round_idx:02d}.bin")
    with open(raw_path, "wb") as f:
        f.write(buf)

    ok = len(buf) == ROUND_BYTES
    print(f"[round {round_idx}] 受信 {len(buf)}/{ROUND_BYTES} bytes, {elapsed:.1f}s, ok={ok}", flush=True)

    if not ok:
        return None, False

    unique = bytearray()
    for i in range(0, len(buf), BANK_SIZE):
        unique += buf[i:i + UNIQUE_HALF]
    uniq_path = os.path.join(OUT_DIR, f"unique_{round_idx:02d}.bin")
    with open(uniq_path, "wb") as f:
        f.write(unique)

    return bytes(unique), True


def checksum_check(data):
    off = 0x7fc0
    if len(data) < off + 32:
        return False, None, None
    cs = data[off + 28] | (data[off + 29] << 8)
    cc = data[off + 30] | (data[off + 31] << 8)
    computed = sum(data) & 0xFFFF
    valid = (computed == cs) and (((cs + cc) & 0xFFFF) == 0xFFFF)
    return valid, computed, cs


def majority_merge(samples):
    n = len(samples[0])
    merged = bytearray(n)
    mismatches = []
    for i in range(n):
        counter = Counter(s[i] for s in samples)
        best_byte, best_count = counter.most_common(1)[0]
        merged[i] = best_byte
        if best_count < len(samples):
            mismatches.append((i, best_count, len(samples), dict(counter)))
    return bytes(merged), mismatches


def write_report(samples, merged, mismatches, valid, computed, expected):
    report_path = os.path.join(OUT_DIR, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"有効サンプル数: {len(samples)}\n")
        f.write(f"チェックサム一致: {valid} 計算値={hex(computed) if computed is not None else 'NA'} "
                f"期待値={hex(expected) if expected is not None else 'NA'}\n")
        f.write(f"多数決で割れたバイト数: {len(mismatches)}\n")
        for pos, best_count, total, counter in mismatches[:300]:
            f.write(f"  offset={hex(pos)} best={best_count}/{total} votes={counter}\n")
    print(f"レポート出力: {report_path}", flush=True)


def main():
    samples = []
    round_idx = 0
    target = TARGET_ROUNDS

    while round_idx < MAX_ROUNDS:
        round_idx += 1
        print(f"=== round {round_idx} 開始 ===", flush=True)
        data, ok = run_one_dump(round_idx)
        if ok:
            samples.append(data)
        print(f"=== round {round_idx} 終了、有効サンプル数: {len(samples)} ===", flush=True)

        if len(samples) >= target:
            merged, mismatches = majority_merge(samples)
            valid, computed, expected = checksum_check(merged)
            print(f"[マージ検証] 有効サンプル={len(samples)} checksum_valid={valid} "
                  f"computed={hex(computed) if computed else 'NA'} "
                  f"expected={hex(expected) if expected else 'NA'} "
                  f"割れたバイト数={len(mismatches)}", flush=True)

            if valid:
                final_path = os.path.join(OUT_DIR, "MERGED_FINAL.sfc")
                with open(final_path, "wb") as f:
                    f.write(merged)
                write_report(samples, merged, mismatches, valid, computed, expected)
                print(f"成功: {final_path} を保存しました。", flush=True)
                return
            else:
                target = len(samples) + EXTEND_STEP
                print(f"チェックサム不一致のため目標を{target}回に延長して継続します。", flush=True)

    print(f"MAX_ROUNDS({MAX_ROUNDS})に到達。ベストエフォートで保存します。", flush=True)
    merged, mismatches = majority_merge(samples)
    valid, computed, expected = checksum_check(merged)
    final_path = os.path.join(OUT_DIR, "MERGED_BESTEFFORT.sfc")
    with open(final_path, "wb") as f:
        f.write(merged)
    write_report(samples, merged, mismatches, valid, computed, expected)
    print(f"ベストエフォート版を保存: {final_path} (checksum_valid={valid})", flush=True)


if __name__ == "__main__":
    main()
