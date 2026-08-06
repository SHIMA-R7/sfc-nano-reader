"""
同じバンクを複数回読み、バイトごとの多数決で確定させる。

■ どういうときに使うのか
通常のダンプ(dump_by_bank.py)は「2回読んで完全一致するまで繰り返す」方式で、これは
読み出しの誤りが稀にしか起きない場合に有効。しかし相手が**動作中のバスマスタ**だと、
そもそも2回一致が永久に成立しないことがある。

Super FX(GSU)搭載カートがこれ。GSUは自分でROMバスを使うため、こちらの読み出しと衝突して
毎回違う場所が化ける。実測ではスターフォックスのバンク0で、最速設定でも65,536バイト中
75〜89バイトが毎回変わった。

■ なぜ多数決が効くのか
誤りの位置がランダムなら、同じ場所が何度も続けて化ける確率は低い。バイトごとに多数決を
取れば正しい値が浮かび上がる。そして**チェックサムが答え合わせをしてくれる**ので、
「それらしい値をでっち上げた」だけで終わらない。

■ 遅くしても直らない
Super FXでは待ち時間を伸ばすと**かえって悪化する**（5us:89バイト → 300us:9828バイト）。
1回の読み出しに時間をかけるほどGSUが割り込む機会が増えるため。原因がセトリング時間では
ないことの証拠でもあるので、この用途では最速設定のまま回数で殴る。

■ 外れ値の除外
稀に「途中から全く別物になったサンプル」が出る（実測で3〜4万バイト相違）。素朴な多数決だと
こういう1本が結果を汚すので、中央値から極端に外れたサンプルは捨ててから集計する。

    python dump_voting.py --port COM12 --banks 16 --samples 5 --mapping linear --out StarFox.sfc
"""

import argparse
import collections
import glob
import os
import sys
import time
import zlib

from bankio import BANK_SIZE, TIMING_TIERS, _read_bank_once


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--banks", type=int, required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--samples", type=int, default=5,
                   help="1バンクあたり最終的に何本のサンプルを揃えるか (既定5)。"
                        "生サンプルはキャッシュに残るので、あとから大きい値で再実行すれば"
                        "不足分だけ読み足す。5で走らせた後に10を指定すれば5本追加になる")
    p.add_argument("--start-bank", default="0")
    p.add_argument("--mapping", choices=["lorom", "hirom", "linear"], default="linear",
                   help="lorom は各バンクの上位32KBだけ採る。hirom/linear は64KBフル")
    p.add_argument("--mirror-halves", action="store_true",
                   help="下位32KBが上位のミラーになっているカートで、1回の読み出しから"
                        "サンプルを2つ取り出す。実質サンプル数が倍になる")
    p.add_argument("--prefer-nonff", action="store_true",
                   help="0xFFを「読めなかった印」とみなし、非0xFFの最頻値を優先する。"
                        "バスを奪い合う相手(Super FXのGSU等)がいる場合に有効。"
                        "本物の0xFFは全サンプルで0xFFになるので誤らない")
    p.add_argument("--cache", default=None, help="生サンプルの保存先 (既定: <out>.votes)")
    a = p.parse_args()
    a.start_bank = int(str(a.start_bank), 0)
    return a


def vote(samples, prefer_nonff=False):
    """バイトごとの多数決。外れ値のサンプルを捨ててから集計する。

    prefer_nonff=True では 0xFF を「読めなかった印」として扱い、非0xFFの最頻値を優先する。
    Super FXのように相手がバスを握る場合、奪われた読みは0xFFになるため、たとえ0xFFが
    多数派でも1本でも実データが取れていればそちらが正しい。本物の0xFFなら全サンプルが
    0xFFになるので、この優先規則で壊れることはない。

    戻り値は (確定データ, 採用サンプル数, 過半数に届かなかったバイト数)。
    """
    if len(samples) == 1:
        return samples[0], 1, 0

    def merge(pool):
        out = bytearray(len(pool[0]))
        weak = 0
        for i in range(len(out)):
            vals = [s[i] for s in pool]
            c = collections.Counter(vals)
            if prefer_nonff:
                nz = collections.Counter(v for v in vals if v != 0xFF)
                if nz:
                    v, n = nz.most_common(1)[0]
                    out[i] = v
                    if n * 2 <= sum(nz.values()):
                        weak += 1
                    continue
            v, n = c.most_common(1)[0]
            out[i] = v
            if n * 2 <= len(pool):
                weak += 1
        return bytes(out), weak

    rough, _ = merge(samples)
    diffs = [sum(1 for a, b in zip(s, rough) if a != b) for s in samples]
    median = sorted(diffs)[len(diffs) // 2]
    # 中央値の10倍を超えて外れたものは「途中で別物になったサンプル」とみなして捨てる。
    # ただし捨てすぎて3本を切ると多数決が成立しないので、そこで打ち止め。
    limit = max(median * 10, 64)
    keep = [s for s, d in zip(samples, diffs) if d <= limit]
    if len(keep) < 3:
        keep = samples
    merged, weak = merge(keep)
    return merged, len(keep), weak


def extract(raw, mapping):
    if mapping == "lorom":
        half = BANK_SIZE // 2
        out = bytearray()
        for i in range(0, len(raw), BANK_SIZE):
            out += raw[i + half:i + BANK_SIZE]
        return bytes(out)
    return raw


def verify(rom):
    """ヘッダ位置を決め打ちせず、補数対が成立する方を採用する。"""
    for off in (0x7FC0, 0xFFC0):
        if len(rom) < off + 32:
            continue
        comp = rom[off + 28] | (rom[off + 29] << 8)
        csum = rom[off + 30] | (rom[off + 31] << 8)
        if ((csum + comp) & 0xFFFF) == 0xFFFF and csum:
            return off, csum, sum(rom) & 0xFFFF
    return None, None, sum(rom) & 0xFFFF


def main():
    args = parse_args()
    cache = args.cache or (args.out + ".votes")
    os.makedirs(cache, exist_ok=True)

    start = time.time()
    banks = []
    for i in range(args.banks):
        b = args.start_bank + i

        # 生サンプルを1本ずつ別ファイルで残す。集計結果だけを保存すると、あとから
        # 読み足して精度を上げることができず、40分かけた読み出しを毎回捨てることになる。
        existing = sorted(glob.glob(os.path.join(cache, f"bank_{b:03d}_s*.bin")))
        samples = []
        for f in existing:
            if os.path.getsize(f) == BANK_SIZE:
                with open(f, "rb") as fh:
                    samples.append(fh.read())

        need = args.samples - len(samples)
        if need > 0:
            if samples:
                print(f"bank ${b:02X}: 既存{len(samples)}本 + {need}本を追加読み", flush=True)
            for n in range(need):
                data = _read_bank_once(args.port, b, TIMING_TIERS[0],
                                       log=lambda m: print(m, flush=True))
                if data is None:
                    continue
                idx = len(samples)
                with open(os.path.join(cache, f"bank_{b:03d}_s{idx:02d}.bin"), "wb") as fh:
                    fh.write(data)
                samples.append(data)
        if not samples:
            print(f"bank ${b:02X}: 1回も読めませんでした。中断します。", flush=True)
            return 1

        pool = samples
        if args.mirror_halves:
            half = BANK_SIZE // 2
            pool = [h for s in samples for h in (s[:half], s[half:])]
        merged, used, weak = vote(pool, prefer_nonff=args.prefer_nonff)
        uniq = len(set(merged))
        print(f"bank ${b:02X}: サンプル{len(samples)}本({len(pool)}系列) → {used}本採用 / "
              f"過半数未達 {weak} バイト / 異なる値 {uniq}種", flush=True)
        if uniq <= 1:
            print(f"  ※ 全バイトが同一値です。この窓には何も応答していません。", flush=True)
        banks.append(merged)

    rom = b"".join(banks) if args.mirror_halves else extract(b"".join(banks), args.mapping)
    off, expected, computed = verify(rom)
    crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")

    print(f"\n合計 {len(rom)} bytes / 所要 {time.time() - start:.0f}秒", flush=True)
    if off is None:
        print("有効なヘッダが見つかりませんでした。", flush=True)
        ok = False
    else:
        ok = computed == expected
        print(f"ヘッダ位置 0x{off:04X} / 計算値=0x{computed:04x} 期待値=0x{expected:04x}"
              f" -> {'一致' if ok else '不一致'}", flush=True)
    print(f"CRC32 = {crc}", flush=True)

    out = args.out if ok else args.out + ".unverified"
    with open(out, "wb") as f:
        f.write(rom)
    print(f"保存: {out}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
