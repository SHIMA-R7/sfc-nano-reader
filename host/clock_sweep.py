# -*- coding: utf-8 -*-
"""カート1番へ与えるクロックを変えて、SA-1が読めるかを調べる。

■ 何を知りたいか
「クロックを与えるとSA-1が暴れる」のは**速すぎるから**なのか、
**クロックの有無そのもの**なのか。

sanniは SA-1 の解錠に 4MHz を使っている（21.477MHzではない）。
    set_freq(400000000ULL, SI5351_CLK0);   // EXT 4 MHz
    // Set clocks to 4Mhz/1Mhz for better SA-1 unlocking
中間の周波数が一度も試されていないので、そこを埋める。

■ 基準は「ハイインピーダンス」であって「LOW固定」ではない
実測でここが分かれた（カービィSDX、各3回）。

    D10 = LOW固定         読めた 0/3   （電流 92mA）
    D10 = ハイインピーダンス  読めた 3/3   （電流 72mA）

「クロックを与えない」とは駆動しないことで、LOWに落とすことではない。

■ 判定
起動読みのあと $C0 を1バンク読み、既知の正解の先頭64KBと一致するかを見る。
1回25秒で済む。全64バンクの確認は、通った周波数だけ後で行う。

    python host/clock_sweep.py --port COM19 --uno COM17 --ref KirbySDX.sfc
"""
import argparse, collections, csv, io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, TIMING_TIERS
from dump_sa1 import burst
from sa1_wake import wake
from dp100 import DP100

# (コマンド, 表示名)。Timer1のCTCトグルは 16MHz/(2*(n+1))。
STEPS = [(b"z", "ハイインピーダンス"), (b"x", "LOW固定"),
         (b"7", "1.000 MHz"), (b"3", "2.000 MHz"), (b"2", "2.667 MHz"),
         (b"1", "4.000 MHz"), (b"0", "8.000 MHz")]

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True, help="Nano-2")
    a.add_argument("--uno", required=True, help="uno_clockgen")
    a.add_argument("--ref", required=True)
    a.add_argument("--trials", type=int, default=5)
    a.add_argument("--csv", default="clock_sweep.csv")
    a.add_argument("--only", help="実行する条件をコマンド文字で絞る。例 z21")
    g = a.parse_args()
    ref = io.open(g.ref, "rb").read()[:BANK_SIZE]
    T = TIMING_TIERS[0][:3]
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    u = serial.Serial(g.uno, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    p = DP100()
    new = not os.path.exists(g.csv)
    f = io.open(g.csv, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new:
        w.writerow(["iso","clk_cmd","clk_label","trial","uniq","match","i_mA","wake_alive"])
    try:
        steps = [(c, l) for c, l in STEPS
                 if not g.only or c.decode() in g.only]
        for cmd, label in steps:
            good = 0
            for t in range(1, g.trials + 1):
                u.write(cmd); time.sleep(0.3)
                st = u.readline().decode("ascii", "replace").strip()
                p.apply(False, 5000, 1000); time.sleep(5.0)
                p.apply(True, 5000, 1000);  time.sleep(3.0)
                dl = time.time() + 25
                while time.time() < dl and g.port not in [q.device for q in lp.comports()]:
                    time.sleep(0.3)
                time.sleep(2.0)
                alive = wake(g.port, T, lambda m: None)
                r = burst(g.port, 0xC0, 1, T, True, lambda m: None)
                ma = p.status()["iout_mA"]
                uniq = len(collections.Counter(r)) if r else 0
                match = int(bool(r) and r == ref)
                good += match
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), cmd.decode(), label,
                            t, uniq, match, ma, " ".join(alive)])
                f.flush()
                log("  %-16s 試行%d  %s  種類%4d  %3.0fmA  %s"
                    % (label, t, "○" if match else "×", uniq, ma, st))
            log("=== %-16s %d/%d ===" % (label, good, g.trials))
        u.write(b"z")      # 終わったら手を離す
        return 0
    finally:
        f.close(); u.close(); p.close()

if __name__ == "__main__":
    sys.exit(main())
