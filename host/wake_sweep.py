# -*- coding: utf-8 -*-
"""起動読み（ダミー読み）の中身を変えて、何が効いているのかを調べる。

■ 背景
SA-1は「いきなり64バンク読みに行くとROMを出さない」。先に短い読み出しを
何度かすると開く。だが**どの要素が効いているのか分かっていない。**

    バンクを散らすこと？  回数？  長さ？  接続を張り直すこと？

sanniのダミー読みは「バンク$C0固定で1024アドレスを1回」で、
**我々のファームは既にそれを実装している(prime)。にもかかわらず45回失敗した。**
つまりsanni方式では足りないはずだが、それは観察であって実験ではない。ここで確かめる。

■ 条件
    none      起動読みなし。**何もしなければ本当に失敗するのか**の基準
    sanni     [0xC0] を1回。sanni相当。primeフラグは常に立っているので実質prime2回
    c0x6      [0xC0] を6回。バンクを散らさず回数だけ増やす
    e0only    [0xE0] を1回。「$E0が入口」説の検証
    full      $00 $20 $40 $80 $C0 $E0（現行）

■ 判定
起動読みのあと $C0 を1バンク読み、既知の正解の先頭64KBと一致するか。
1回25秒。クロックは**ハイインピーダンス固定**（LOW固定は0/9で駄目と判明済み）。

    python host/wake_sweep.py --port COM19 --uno COM17 --ref KirbySDX.sfc --trials 20
"""
import argparse, collections, csv, io, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, TIMING_TIERS
from dump_sa1 import burst
from dp100 import DP100

PATTERNS = collections.OrderedDict([
    ("none",   []),
    ("sanni",  [0xC0]),
    ("c0x6",   [0xC0] * 6),
    ("e0only", [0xE0]),
    ("full",   [0x00, 0x20, 0x40, 0x80, 0xC0, 0xE0]),
])

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--uno", required=True)
    a.add_argument("--ref", required=True)
    a.add_argument("--trials", type=int, default=20)
    a.add_argument("--only", help="条件名をカンマ区切りで絞る")
    a.add_argument("--csv", default="wake_sweep.csv")
    g = a.parse_args()
    ref = io.open(g.ref, "rb").read()[:BANK_SIZE]
    T = TIMING_TIERS[0][:3]
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    u = serial.Serial(g.uno, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    u.write(b"z"); time.sleep(0.3)          # **クロックはハイインピーダンス固定**
    log("Uno: " + u.readline().decode("ascii", "replace").strip())
    p = DP100()
    new = not os.path.exists(g.csv)
    f = io.open(g.csv, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new:
        w.writerow(["iso", "pattern", "banks", "trial", "uniq", "match", "i_mA", "alive"])
    names = [n for n in PATTERNS if not g.only or n in g.only.split(",")]
    try:
        for name in names:
            banks = PATTERNS[name]; good = 0
            for t in range(1, g.trials + 1):
                p.apply(False, 5000, 1000); time.sleep(5.0)
                p.apply(True, 5000, 1000);  time.sleep(3.0)
                dl = time.time() + 25
                while time.time() < dl and g.port not in [q.device for q in lp.comports()]:
                    time.sleep(0.3)
                time.sleep(2.0)
                alive = []
                for b in banks:
                    r = burst(g.port, b, 1, T, True, lambda m: None)
                    if r is not None and len(collections.Counter(r)) > 50:
                        alive.append("$%02X" % b)
                r = burst(g.port, 0xC0, 1, T, True, lambda m: None)
                ma = p.status()["iout_mA"]
                uniq = len(collections.Counter(r)) if r else 0
                match = int(bool(r) and r == ref); good += match
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), name, len(banks),
                            t, uniq, match, ma, " ".join(alive)])
                f.flush()
                log("  %-7s 試行%-2d %s 種類%4d %3.0fmA %s"
                    % (name, t, "○" if match else "×", uniq, ma, " ".join(alive)))
            log("=== %-7s %d/%d ===" % (name, good, g.trials))
        return 0
    finally:
        f.close(); u.close(); p.close()

if __name__ == "__main__":
    sys.exit(main())
