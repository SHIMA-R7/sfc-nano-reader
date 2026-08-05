"""
バンク単位でダンプする。1バンクごとにNanoをリセットして読み直す。

長時間連続で読み続けると読み取りが化けるが、1バンク(64KB)だけを起動直後に読む分には
再現性が高い。それを利用して、バンクごとに「2回読んで完全一致するまで繰り返す」形で
確実なデータだけを積み上げる。

必要なタイミングマージンはROMチップの個体差でかなり違う（bankio.py参照）ため、
最速の設定から始めて、駄目なら段階的に遅く安全な設定へ上げていく。

    python dump_by_bank.py --port COM12 --banks 32 --mapping hirom --out MyGame.sfc
"""

import argparse
import os
import sys
import time

from bankio import BANK_SIZE, AdaptiveTiming, read_bank_confirmed


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--banks", type=int, required=True)
    p.add_argument("--mapping", choices=["hirom", "lorom"], required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--cache", default=None, help="確定したバンクの保存先 (既定: <out>.banks)")
    return p.parse_args()


def extract_rom(raw, mapping):
    """生の64KB×Nバンクから、ROM本体を取り出す。

    LoROMではROMは各バンクの $8000-$FFFF にしか現れない。カートによっては下位32KBが
    上位のミラーになる（Super Puyo Puyo はこれ）が、下位が一切駆動されず 0x00 で読める
    カートもある（Super Mario Collection はこれ）。**上位32KBを採るのが常に正しい。**
    """
    if mapping == "hirom":
        return raw
    out = bytearray()
    half = BANK_SIZE // 2
    for i in range(0, len(raw), BANK_SIZE):
        out += raw[i + half:i + BANK_SIZE]
    return bytes(out)


def verify(rom, mapping):
    off = 0xFFC0 if mapping == "hirom" else 0x7FC0
    if len(rom) < off + 32:
        return False, None, None
    complement = rom[off + 28] | (rom[off + 29] << 8)
    checksum = rom[off + 30] | (rom[off + 31] << 8)
    if ((checksum + complement) & 0xFFFF) != 0xFFFF:
        return False, None, checksum
    computed = sum(rom) & 0xFFFF
    return computed == checksum, computed, checksum


def main():
    args = parse_args()
    cache = args.cache or (args.out + ".banks")
    os.makedirs(cache, exist_ok=True)

    dump_start = time.time()
    read_count = 0  # 新規に読んだバンク数（キャッシュ済みは除く）
    adaptive = AdaptiveTiming()

    banks = []
    for b in range(args.banks):
        path = os.path.join(cache, f"bank_{b:03d}.bin")
        if os.path.exists(path) and os.path.getsize(path) == BANK_SIZE:
            with open(path, "rb") as f:
                banks.append(f.read())
            print(f"bank {b:3d}: キャッシュ済み", flush=True)
            continue

        print(f"bank {b:3d}: 読み出し中", flush=True)
        bank_start = time.time()
        data, tier_idx, tier = read_bank_confirmed(
            args.port, b, total_banks=args.banks, start_idx=adaptive.next_start_idx(),
            log=lambda m: print(m, flush=True))
        if data is None:
            print(f"bank {b:3d}: 一致を得られませんでした。中断します。", flush=True)
            return 1
        adaptive.report(tier_idx, log=lambda m: print(m, flush=True))
        print(f"bank {b:3d}: 完了 [{tier}] ({time.time() - bank_start:.1f}秒)", flush=True)
        with open(path, "wb") as f:
            f.write(data)
        banks.append(data)
        read_count += 1

    dump_elapsed = time.time() - dump_start

    raw = b"".join(banks)
    rom = extract_rom(raw, args.mapping)
    ok, computed, expected = verify(rom, args.mapping)
    print(f"\n合計 {len(rom)} bytes / 計算値={hex(computed) if computed is not None else 'NA'} "
          f"期待値={hex(expected) if expected is not None else 'NA'} -> "
          f"{'一致' if ok else '不一致'}", flush=True)

    if read_count:
        speed = (read_count * BANK_SIZE) / dump_elapsed / 1024
        print(f"所要時間 = {dump_elapsed:.1f}秒 (新規読み出し{read_count}バンク, "
              f"平均{speed:.1f} KB/s)", flush=True)
    else:
        print(f"所要時間 = {dump_elapsed:.1f}秒（全バンクがキャッシュ済みでした）", flush=True)

    out = args.out if ok else args.out + ".unverified"
    with open(out, "wb") as f:
        f.write(rom)
    print(f"保存: {out}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
