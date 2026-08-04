"""
Nano-2から複数回ダンプを取得し、"バンク単位"(32KBまるごと)の多数決でマージする。
(v1のバイト単位多数決は、バンク転送が丸ごと化ける失敗モードに対して収束が遅かったため変更)

- 1回の生ダンプは2,097,152バイト(32バンク x 64KB、前半32KBが実データ・後半はミラー)
- 既存の dumps/unique_*.bin (前回までの結果) があれば再利用してサンプル数を稼ぐ
- 各バンク番号ごとに、全サンプル中で最も多く一致した32KBチャンクを採用
- 全32バンクがチェックサム的に正しい組み合わせになったら完成
"""

import os
import sys
import time
import glob
import hashlib
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

MAX_ROUNDS = 80
CHECK_EVERY = 3  # 何ラウンドごとにマージ判定するか


def run_one_dump(round_idx):
    for attempt in range(3):
        try:
            ser = serial.Serial(PORT, BAUD, timeout=25)
            break
        except Exception as e:
            print(f"[round {round_idx}] ポートオープン失敗 (試行{attempt+1}/3): {e}", flush=True)
            time.sleep(3)
    else:
        return None, False

    time.sleep(2)
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

    ok = len(buf) == ROUND_BYTES
    print(f"[round {round_idx}] 受信 {len(buf)}/{ROUND_BYTES} bytes, {elapsed:.1f}s, ok={ok}", flush=True)
    if not ok:
        return None, False

    raw_path = os.path.join(OUT_DIR, f"raw2_{round_idx:02d}.bin")
    with open(raw_path, "wb") as f:
        f.write(buf)

    unique = bytearray()
    for i in range(0, len(buf), BANK_SIZE):
        unique += buf[i:i + UNIQUE_HALF]
    uniq_path = os.path.join(OUT_DIR, f"unique2_{round_idx:02d}.bin")
    with open(uniq_path, "wb") as f:
        f.write(unique)

    return bytes(unique), True


def load_existing_samples():
    samples = []
    for path in sorted(glob.glob(os.path.join(OUT_DIR, "unique*_*.bin"))):
        with open(path, "rb") as f:
            data = f.read()
        if len(data) == NUM_BANKS * UNIQUE_HALF:
            samples.append(data)
    print(f"既存サンプルを{len(samples)}個読み込みました。", flush=True)
    return samples


def checksum_check(data):
    off = 0x7fc0
    if len(data) < off + 32:
        return False, None, None
    cs = data[off + 28] | (data[off + 29] << 8)
    cc = data[off + 30] | (data[off + 31] << 8)
    computed = sum(data) & 0xFFFF
    valid = (computed == cs) and (((cs + cc) & 0xFFFF) == 0xFFFF)
    return valid, computed, cs


def bank_of(data, b):
    s = b * UNIQUE_HALF
    return data[s:s + UNIQUE_HALF]


def per_bank_majority_merge(samples):
    merged = bytearray()
    vote_report = []
    for b in range(NUM_BANKS):
        chunks = [bank_of(s, b) for s in samples]
        counter = Counter(chunks)
        best_chunk, best_count = counter.most_common(1)[0]
        merged += best_chunk
        vote_report.append((b, best_count, len(samples), len(counter)))
    return bytes(merged), vote_report


def main():
    samples = load_existing_samples()
    round_idx = len(samples)

    while round_idx < MAX_ROUNDS:
        round_idx += 1
        print(f"=== round {round_idx} 開始 ===", flush=True)
        data, ok = run_one_dump(round_idx)
        if ok:
            samples.append(data)
        print(f"=== round {round_idx} 終了、有効サンプル数: {len(samples)} ===", flush=True)

        if len(samples) >= 3 and len(samples) % CHECK_EVERY == 0:
            merged, vote_report = per_bank_majority_merge(samples)
            valid, computed, expected = checksum_check(merged)
            weak_banks = [v for v in vote_report if v[1] < (v[2] // 2 + 1)]
            print(f"[マージ判定] サンプル数={len(samples)} checksum_valid={valid} "
                  f"computed={hex(computed) if computed else 'NA'} "
                  f"expected={hex(expected) if expected else 'NA'} "
                  f"過半数未満のバンク数={len(weak_banks)}", flush=True)
            for b, cnt, total, variants in vote_report:
                if cnt < total:
                    print(f"    bank{b}: 最多得票 {cnt}/{total} (バリエーション数={variants})", flush=True)

            if valid:
                final_path = os.path.join(OUT_DIR, "MERGED_FINAL_v2.sfc")
                with open(final_path, "wb") as f:
                    f.write(merged)
                print(f"成功: {final_path} を保存しました。checksum一致。", flush=True)
                return

    print(f"MAX_ROUNDS({MAX_ROUNDS})到達。ベストエフォートで保存します。", flush=True)
    merged, vote_report = per_bank_majority_merge(samples)
    valid, computed, expected = checksum_check(merged)
    final_path = os.path.join(OUT_DIR, "MERGED_BESTEFFORT_v2.sfc")
    with open(final_path, "wb") as f:
        f.write(merged)
    print(f"ベストエフォート版を保存: {final_path} (checksum_valid={valid} "
          f"computed={hex(computed) if computed else 'NA'} expected={hex(expected) if expected else 'NA'})",
          flush=True)


if __name__ == "__main__":
    main()
