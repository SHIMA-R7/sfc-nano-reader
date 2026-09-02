# -*- coding: utf-8 -*-
"""SA-1のセーブデータ(BW-RAM)を読む。

■ 仕組み
SA-1はBW-RAMを直接は見せない。**レジスタに書き込んで窓を開ける**必要がある。
sanni の Cart_Reader/SNES.ino より:

    // Direct writes to BW-RAM (SRAM) in banks 0x40-0x43 don't work
    writeBank_SNES(0, 0x2224, block);   // BMAPS: 8KBブロックを $6000-$7FFF へ
    writeBank_SNES(0, 0x2226, 0x80);    // SBWE:  書き込み許可
    writeBank_SNES(0, 0x2228, 0);       // BWPA:  保護解除
    → その後 $00:6000-7FFF を読む

**バンク$40を直接読んでも取れない。** 実際に試して 0xFF 一色だった。

■ 書き込みの経路
    ホスト → Nano-2（flags bit6 で書き込みモード）
           → 長いストローブ
           → Nano-1 がそれを見て /WR を1パルス出す（カート54番）

Nano-1 はPCと通信できないので、STROBEの幅を合図に使っている。
STROBE立ち上がりでアドレスを+1したあと、まだHIGHなら書き込み指示。
**アドレスを進めた直後なので目的の番地に書ける。**

■ 危険性
**/WR を実際に動かす。書き込み先を間違えるとセーブが壊れる。**
既定ではSA-1のレジスタ3つ以外を弾く。--force で外せるが、通常は使わないこと。

    python host/sa1_bwram.py --port COM19 --out SMRPG.srm
"""
import argparse, collections, io, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
from bankio import BANK_SIZE, BAUD, TIMING_TIERS
from dump_sa1 import burst

# 触ってよいレジスタ。ここ以外への書き込みは既定で拒否する。
SA1_REGS = {0x2224: "BMAPS (窓に割り当てるブロック)",
            0x2226: "SBWE  (書き込み許可)",
            0x2228: "BWPA  (保護領域)"}
WINDOW_LO, WINDOW_HI = 0x6000, 0x8000      # BW-RAMが見える窓
BLOCK = WINDOW_HI - WINDOW_LO              # 8KB

def _open(port):
    s = serial.Serial(port, BAUD, timeout=60)
    dl = time.time() + 20
    while time.time() < dl:
        if s.read(1) == b"R":
            return s
    s.close()
    return None

def write_reg(port, addr, data, force=False, log=print):
    """1バイト書く。**既定ではSA-1のレジスタ以外を拒否する。**"""
    if addr not in SA1_REGS and not force:
        raise ValueError("$%04X への書き込みは許可されていません（--force が要ります）" % addr)
    s = _open(port)
    if s is None:
        log("  ポートが準備完了(R)を返しません"); return False
    try:
        rd, ad, pl = TIMING_TIERS[0][:3]
        # 書き込みモードではヘッダの意味が変わる
        #   [0..1]=アドレス [2]=値 [3]=繰り返し [8]=flags(0x40)
        s.write(bytes([addr & 0xFF, (addr >> 8) & 0xFF, data, 1,
                       ad & 0xFF, ad >> 8, pl & 0xFF, pl >> 8, 0x40, 1, 1]))
        s.flush()
        r = s.read(1)
        ok = (r == b"W")
        log("  $%04X <- 0x%02X  %s%s" % (addr, data, "OK" if ok else "応答なし",
                                         "  " + SA1_REGS.get(addr, "") if addr in SA1_REGS else ""))
        return ok
    finally:
        s.close()

def read_window(port, log=print):
    """$00:6000-7FFF を読む。SRAMモード(0x01)で /ROMSEL を上げたまま読む。"""
    rd, ad, pl = TIMING_TIERS[0][:3]
    s = _open(port)
    if s is None: return None
    try:
        s.write(bytes([0x00, 0, rd & 0xFF, rd >> 8, ad & 0xFF, ad >> 8,
                       pl & 0xFF, pl >> 8, 0x21, 1, 1]))   # prime + SRAM
        s.flush()
        buf = bytearray()
        while len(buf) < BANK_SIZE:
            c = s.read(min(8192, BANK_SIZE - len(buf)))
            if not c: return None
            buf += c
        return bytes(buf[WINDOW_LO:WINDOW_HI])
    finally:
        s.close()

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--blocks", type=int, default=4, help="8KBブロック数。32KBなら4")
    a.add_argument("--force", action="store_true", help="レジスタ以外への書き込みを許す")
    g = a.parse_args()
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)
    T = TIMING_TIERS[0][:3]

    log("起動読み（SA-1を起こす）")
    burst(g.port, 0xE0, 1, T, True, lambda m: None)

    log("BW-RAMの窓を開ける")
    if not write_reg(g.port, 0x2226, 0x80, g.force, log): log("  ** SBWE の応答なし **")
    if not write_reg(g.port, 0x2228, 0x00, g.force, log): log("  ** BWPA の応答なし **")

    out = bytearray()
    for blk in range(g.blocks):
        write_reg(g.port, 0x2224, blk, g.force, log)
        time.sleep(0.05)
        d = read_window(g.port, log)
        if d is None:
            log("  ブロック%d 読めず" % blk); return 1
        c = collections.Counter(d); v, n = c.most_common(1)[0]
        log("  ブロック%d: 種類%4d 最頻 0x%02x %.0f%%" % (blk, len(c), v, 100*n/len(d)))
        out += d
    io.open(g.out, "wb").write(bytes(out))
    log("保存: %s  %d bytes" % (g.out, len(out)))

    c = collections.Counter(out); v, n = c.most_common(1)[0]
    if len(c) < 4:
        log("**一色に近い。窓が開いていない可能性が高い。**")
        return 1
    log("種類 %d / 最頻 0x%02x %.1f%%" % (len(c), v, 100*n/len(out)))
    return 0

if __name__ == "__main__":
    sys.exit(main())
