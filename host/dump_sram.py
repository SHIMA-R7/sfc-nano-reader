"""
カートリッジのセーブデータ(バッテリーバックアップSRAM)を吸い出す。

ROMは焼き直せば同じものが手に入るが、セーブデータは世界に1つしかない。そして
カート内のボタン電池は寿命が近い。切れた瞬間に永久に失われるので、ROMより優先度が高い。

■ どこを読むのか
SRAMはROMとは別の領域にマップされている。
    LoROM: バンク $70〜$7D の $0000-$7FFF
    HiROM: バンク $20〜$3F の $6000-$7FFF
どちらも1バンク内に収まるので、目的のバンクを1つ読めば足りる。

■ /ROMSEL の扱いはマッピングで逆になる（実測で判明）
SNESの /CART(=/ROMSEL) がアサートされる条件は「バンク $40-$7D は全アドレス、
バンク $00-$3F は $8000-$FFFF のみ」。したがって:

    LoROM SRAM ($70:0000-$7FFF) … バンクが $40-$7D 側なので /ROMSEL はアサートする
    HiROM SRAM ($20:6000-$7FFF) … バンクが $00-$3F 側の $8000未満なのでアサートしない

当初は「SRAMなら常に /ROMSEL を上げる」と考えて実装したが、それだとLoROMで
バスが誰にも駆動されず全バイト0x00になった。実測して逆と判明したもの。

■ 書き込み事故は起こらない
/WR はカート側で +5V に直結してある(README参照)。ファームに何を書こうとSRAMへの
書き込みは物理的に発生しない。救出対象を壊す心配はない。

■ ミラーを検証に使う
SRAMの実容量(2KB〜32KB)はバンクの窓(32KB or 8KB)より小さいことが多く、余った領域には
同じ内容が繰り返し現れる。この繰り返しが一致するかどうかを、そのまま読み取り品質の
検証に使える。2回読んで一致させる従来の方法と合わせて二重に確認する。

    python dump_sram.py --port COM12 --rom ..\..\SFC-ROM\MARIOPAINT.sfc --out MarioPaint.srm
"""

import argparse
import os
import sys

from bankio import BANK_SIZE, TIMING_TIERS, _read_bank_once

# LoROMのSRAMは $70:0000 から、HiROMは $20:6000 から
LOROM_SRAM_BANK = 0x70
HIROM_SRAM_BANK = 0x20
HIROM_SRAM_OFFSET = 0x6000
LOROM_WINDOW = 0x8000   # $0000-$7FFF
HIROM_WINDOW = 0x2000   # $6000-$7FFF


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--out", required=True, help="保存先(.srm)")
    p.add_argument("--rom", help="対応するROMファイル。ヘッダからマッピングとSRAM容量を読む")
    p.add_argument("--mapping", choices=["lorom", "hirom"],
                   help="--rom を渡さない場合に手動指定")
    p.add_argument("--size", type=int, help="SRAM容量(バイト)。--rom を渡さない場合に手動指定")
    p.add_argument("--attempts", type=int, default=6,
                   help="最大何回読んで一致を狙うか (既定6)")
    return p.parse_args()


def read_rom_header(path):
    """ROMのヘッダからマッピング・SRAM容量・タイトルを取り出す。"""
    rom = open(path, "rb").read()
    for off, mapping in ((0x7FC0, "lorom"), (0xFFC0, "hirom")):
        if len(rom) < off + 32:
            continue
        complement = rom[off + 28] | (rom[off + 29] << 8)
        checksum = rom[off + 30] | (rom[off + 31] << 8)
        if ((checksum + complement) & 0xFFFF) != 0xFFFF:
            continue
        title = rom[off:off + 21].decode("shift_jis", errors="replace").strip()
        sram_code = rom[off + 0x18]
        size = (1024 << sram_code) if sram_code else 0
        return mapping, size, title
    return None, 0, None


def extract(raw, mapping, size):
    """バンク1つ分(64KB)の生データから、SRAM本体と検証用のミラーを取り出す。

    戻り値は (本体, [ミラー...])。
    """
    if mapping == "hirom":
        window = raw[HIROM_SRAM_OFFSET:HIROM_SRAM_OFFSET + HIROM_WINDOW]
    else:
        window = raw[:LOROM_WINDOW]
    if size <= 0 or size > len(window):
        return window, []
    body = window[:size]
    mirrors = [window[i:i + size] for i in range(size, len(window) - size + 1, size)]
    return body, mirrors


def main():
    args = parse_args()

    mapping, size, title = (None, 0, None)
    if args.rom:
        mapping, size, title = read_rom_header(args.rom)
        if mapping is None:
            print("ROMのヘッダを認識できませんでした。--mapping と --size で指定してください。")
            return 1
        print(f"ROM: 『{title}』 {mapping.upper()} / SRAM {size // 1024}KB" if size
              else f"ROM: 『{title}』 {mapping.upper()} / SRAMなし")
        if size == 0:
            print("このカートにはセーブ領域がありません。")
            return 1
    mapping = args.mapping or mapping
    size = args.size or size
    if not mapping or not size:
        print("--rom を渡すか、--mapping と --size を指定してください。")
        return 1

    if mapping == "hirom":
        bank, no_romsel = HIROM_SRAM_BANK, True
    else:
        bank, no_romsel = LOROM_SRAM_BANK, False
    print(f"バンク ${bank:02X} を読みます（{mapping.upper()} / {size}バイト / "
          f"/ROMSEL {'非アサート' if no_romsel else 'アサート'}）")

    tier = TIMING_TIERS[0]
    prev = None
    for attempt in range(1, args.attempts + 1):
        raw = _read_bank_once(args.port, bank, tier, total_banks=0,
                              log=lambda m: print(m, flush=True), sram=no_romsel)
        if raw is None:
            print(f"  試行{attempt}: 読み出せませんでした")
            continue
        body, mirrors = extract(raw, mapping, size)

        # ミラーの一致は「1回の読みだけで分かる」補助的な手がかりとして扱う。
        # 判定の主軸はあくまで2回読んで一致すること。カートによってはSRAMの外側が
        # オープンバスになりミラーが現れないので、不一致を即失敗にすると誤判定する。
        bad = sum(1 for m in mirrors if m != body)
        if mirrors:
            hint = ("ミラー%d個すべて一致" % len(mirrors) if bad == 0
                    else "ミラー%d/%d個が不一致(この機種では正常な場合あり)" % (bad, len(mirrors)))
        else:
            hint = "ミラーなし"

        if prev is not None and prev == body:
            blank = all(b == body[0] for b in body)
            print(f"  試行{attempt}: 2回一致 / {hint}")
            if blank:
                print(f"  ※ 全バイトが 0x{body[0]:02x} です。"
                      "セーブが空か、電池が切れている可能性があります")
            with open(args.out, "wb") as f:
                f.write(body)
            print(f"保存: {args.out} ({len(body)}バイト)")
            return 0
        if prev is not None:
            diff = sum(1 for a, b in zip(prev, body) if a != b)
            print(f"  試行{attempt}: 前回と {diff} バイト相違 / {hint}")
        else:
            print(f"  試行{attempt}: {hint}。もう1回読んで確認します")
        prev = body

    print("一致を得られませんでした。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
