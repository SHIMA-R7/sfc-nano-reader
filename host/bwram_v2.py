# -*- coding: utf-8 -*-
"""SA-1のセーブ(BW-RAM)を、sanniと同じ手順で読む。

■ 訂正の記録
最初は $2224/$2226/$2228 を書いて窓を開ける実装にした。**誤りだった。**
あれはsanniの**書き込み（復元）側**のコードで、読み出し側はこうなっている。

    } else if (romType == SA) {
        PORTH |= (1 << 3);                            // /ROMSEL を HIGH にするだけ
        myFile.write(readBank_SNES(0x40, currByte));  // **バンク $40 を直接読む**
    }

「Direct writes to BW-RAM in banks 0x40-0x43 don't work」は**書き込みの話**。
読み出しにレジスタ操作は要らない。

■ sanniとの残る差: CICクロック
sanniはCICクロックを**流しっぱなし**にする。
我々のNano-3は認証後に線を解放し「Keyを凍結」していた。
ROMを読むにはそれで足りたが、SA-1を働かせるには足りない可能性がある。

■ 手順（配線の衝突を避ける順序）
    1. clockduino OFF        56番はNano-3だけが駆動
    2. CIC認証を実行          Nano-3が従来どおり握手する
    3. Nano-3がD12を解放      認証の最後に自動で入力へ戻る
    4. clockduino ON          56番を引き継いで流しっぱなしになる
                              （同時にカート1番へ21.477MHzも出る）
    5. バンク $40 を読む
"""
import argparse, collections, io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial, serial.tools.list_ports as lp
from bankio import BANK_SIZE, BAUD, TIMING_TIERS
from dp100 import DP100
from cicauth import reauth

T = TIMING_TIERS[0][:3]

def read_bank(port, bank, flags=0x21):
    """flags 0x21 = prime + SRAMモード(/ROMSEL を上げたまま)。sanniの CS high に相当。"""
    s = serial.Serial(port, BAUD, timeout=60)
    try:
        dl = time.time() + 20
        ok = False
        while time.time() < dl:
            if s.read(1) == b"R": ok = True; break
        if not ok: return None
        rd, ad, pl = T
        s.write(bytes([bank, 0, rd & 0xFF, rd >> 8, ad & 0xFF, ad >> 8,
                       pl & 0xFF, pl >> 8, flags, 1, 1])); s.flush()
        b = bytearray()
        while len(b) < BANK_SIZE:
            c = s.read(min(8192, BANK_SIZE - len(b)))
            if not c: return None
            b += c
        return bytes(b)
    finally:
        s.close()

def describe(d):
    c = collections.Counter(d); v, n = c.most_common(1)[0]
    return "種類%4d 最頻0x%02x %5.1f%%" % (len(c), v, 100.0 * n / len(d))

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", default="COM19")
    a.add_argument("--uno", default="COM17")
    a.add_argument("--out", default="SMRPG.srm")
    a.add_argument("--reads", type=int, default=3, help="安定性を見るための繰り返し")
    g = a.parse_args()
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    u = serial.Serial(g.uno, 115200, timeout=2); time.sleep(2.2)
    u.reset_input_buffer(); u.readline()
    def cmd(c):
        u.write(c); time.sleep(0.4); return u.readline().decode("ascii", "replace").strip()
    p = DP100()
    try:
        log("1. clockduino OFF（56番はNano-3が駆動）: " + cmd(b"w"))
        cmd(b"z")                                  # 57番は触らない
        p.apply(False, 5000, 1000); time.sleep(5)
        p.apply(True, 5000, 1000);  time.sleep(3)
        dl = time.time() + 25
        while time.time() < dl and g.port not in [q.device for q in lp.comports()]:
            time.sleep(0.3)
        time.sleep(2)
        log("   電源 %.3f V" % (p.status()["vout_mV"] / 1000))

        log("2. CIC認証（Nano-3が握手し、終わるとD12を解放する）")
        reauth(g.port, log=lambda m: log("   " + m.strip()))

        log("3. clockduino ON（56番を引き継ぎ、1番にも21.477MHz）: " + cmd(b"v"))
        time.sleep(0.3)

        log("4. バンク $40 を読む（sanniと同じ場所）")
        reads = []
        for i in range(g.reads):
            d = read_bank(g.port, 0x40)
            if d is None:
                log("   読み%d 失敗" % (i + 1)); continue
            reads.append(d)
            log("   読み%d CRC %08x  %s" % (i + 1, zlib.crc32(d) & 0xFFFFFFFF, describe(d)))
        if len(reads) >= 2:
            df = sum(1 for x, y in zip(reads[0], reads[1]) if x != y)
            stable = df < BANK_SIZE * 0.02
            log("   1回目と2回目の相違 %d バイト (%.1f%%) → %s"
                % (df, 100.0 * df / BANK_SIZE, "**安定＝実メモリの可能性**" if stable else "不安定＝ノイズ"))
            if stable and len(collections.Counter(reads[0])) > 4:
                io.open(g.out, "wb").write(reads[0])
                log("   保存: %s" % g.out)
        log("5. 比較のため clockduino OFF で同じ場所を読む: " + cmd(b"w"))
        time.sleep(0.3)
        d = read_bank(g.port, 0x40)
        if d: log("   %s" % describe(d))
        return 0
    finally:
        cmd(b"w"); cmd(b"z"); u.close(); p.close()

if __name__ == "__main__":
    sys.exit(main())
