# -*- coding: utf-8 -*-
"""電源電圧を振って、吸い出しの成否がどう変わるかを測る。

■ 何を知りたいか
スーパーFX(GSU)カートが、電源電圧の低下にどれだけ弱いか。
USB給電で 2/6 しか通らなかったのが電圧のせいなのかを、直接確かめる。

■ 方法
5.00V から 0.1V ずつ 4.50V まで下げ、各電圧で10回ずつ吸う。
1回ごとに電源を切って入れ直すので、試行は独立している。
成否は**既知の正解との一致**で判定する（チェックサムだけでは足りない。
今日、64/64バンクが「ROMらしい」のに中身が3割違う例を何度も見た）。

■ 安全
dp100.py の VMAX_MV=5000 が効いているので、この実験から5Vを超える指定はできない。
下げる方向なので過電圧の危険はない。

    python host/voltage_sweep.py --port COM19 --ref StarFox.sfc
"""
import argparse, csv, io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, TIMING_TIERS
from dump_sa1 import burst
from dp100 import DP100

def extract_lorom(raw):
    """LoROMは各バンクの上位32KBだけがROM本体。"""
    half = BANK_SIZE // 2
    return b"".join(raw[i + half:i + BANK_SIZE] for i in range(0, len(raw), BANK_SIZE))

def rom_sum(b):
    return sum(b) & 0xFFFF

def wait_port(port, timeout_s):
    dl = time.time() + timeout_s
    while time.time() < dl:
        if port in [q.device for q in lp.comports()]:
            time.sleep(2.0); return True
        time.sleep(0.3)
    return False

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--ref", required=True, help="既知の正解ROM")
    a.add_argument("--banks", type=int, default=32)
    a.add_argument("--trials", type=int, default=10)
    a.add_argument("--from-mv", type=int, default=5000)
    a.add_argument("--to-mv", type=int, default=4500)
    a.add_argument("--step-mv", type=int, default=100)
    a.add_argument("--off-seconds", type=float, default=5.0)
    a.add_argument("--csv", default="voltage_sweep.csv")
    g = a.parse_args()

    ref = io.open(g.ref, "rb").read()
    ref_crc = format(zlib.crc32(ref) & 0xFFFFFFFF, "08x")
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)
    log("正解: %s  %d bytes  CRC %s" % (g.ref, len(ref), ref_crc))

    volts = list(range(g.from_mv, g.to_mv - 1, -g.step_mv))
    new = not os.path.exists(g.csv)
    f = io.open(g.csv, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new:
        w.writerow(["iso","v_set_mV","v_meas_mV","i_mA","trial","ok",
                    "crc","checksum","diff_bytes","first_bad_bank","elapsed_s","note"])
    p = DP100()
    try:
        for mv in volts:
            good = 0
            for t in range(1, g.trials + 1):
                p.apply(False, mv, 1000); time.sleep(g.off_seconds)
                p.apply(True, mv, 1000);  time.sleep(0.5)
                s = p.status()
                if s["vout_mV"] > 5300:          # 念のため
                    p.apply(False, mv, 1000)
                    log("出力が高すぎるため中止"); return 1
                if not wait_port(g.port, 25):
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, s["vout_mV"],
                                s["iout_mA"], t, 0, "", "", "", "", "", "ポート復帰せず"])
                    f.flush(); log("  %.2fV 試行%-2d ポートが復帰しません" % (mv/1000, t)); continue
                t0 = time.time()
                raw = burst(g.port, 0x00, g.banks, TIMING_TIERS[0][:3], True, lambda m: None)
                el = time.time() - t0
                s2 = p.status()
                if raw is None:
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, s2["vout_mV"],
                                s2["iout_mA"], t, 0, "", "", "", "", "%.1f"%el, "読み出し失敗"])
                    f.flush(); log("  %.2fV 試行%-2d 読み出し失敗" % (mv/1000, t)); continue
                rom = extract_lorom(raw)
                crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
                ok = int(crc == ref_crc)
                good += ok
                diff = first = ""
                if not ok:
                    d = sum(1 for x, y in zip(rom, ref) if x != y)
                    diff = d
                    fb = next((i for i in range(0, len(rom), 0x8000)
                               if rom[i:i+0x8000] != ref[i:i+0x8000]), None)
                    first = "" if fb is None else fb // 0x8000
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, s2["vout_mV"], s2["iout_mA"],
                            t, ok, crc, "0x%04x" % rom_sum(rom), diff, first, "%.1f"%el, ""])
                f.flush()
                log("  %.2fV 試行%-2d %s CRC %s%s" % (
                    mv/1000, t, "○" if ok else "×", crc,
                    "" if ok else "  相違 %s バイト / 最初に崩れた32KB区画 %s" % (diff, first)))
            log("=== %.2f V : %d/%d 成功 ===" % (mv/1000, good, g.trials))
        p.apply(True, 5000, 1000)
        log("5.00Vに戻しました")
        return 0
    finally:
        f.close(); p.close()

if __name__ == "__main__":
    sys.exit(main())
