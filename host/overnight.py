# -*- coding: utf-8 -*-
"""夜間に無人で回す実験一式。

■ 方針
・**1つの段が転んでも次に進む。** 朝に「全部落ちていた」が最悪なので、
  段ごとに例外を捕まえてCSVを閉じ、次へ行く。
・結果はCSVに1行ずつflushする。途中で止まっても取れた分は残る。
・クロックは**ハイインピーダンス固定**（LOW固定は0/9、4MHzは50%と判明済み）。
・最後に電源を5.00Vへ戻して出力を入れたままにする（朝すぐ続けられるように）。

■ 段取り
  1. ダミー読みの要素分解      5条件 × 20回
  2. 最良手順での電圧掃引      6電圧 × 20回
  3. 起動読みの持続時間        5待機 × 15回
  4. 1バンク判定が全64バンクと一致するかの検証   20回
  5. 基準条件の追試            40回（信頼区間を締める）
"""
import collections, csv, io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, TIMING_TIERS
from dump_sa1 import burst, rom_likeness, header_at
from dp100 import DP100

PORT, UNO = "COM19", "COM17"
REF = io.open(r"C:\SFC-Dumper\KirbySDX.sfc", "rb").read()
REF_HEAD, REF_CRC = REF[:BANK_SIZE], format(zlib.crc32(REF) & 0xFFFFFFFF, "08x")
T = TIMING_TIERS[0][:3]
FULL_WAKE = [0x00, 0x20, 0x40, 0x80, 0xC0, 0xE0]
OUT = r"C:\SFC-Dumper"

def log(m): print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def csv_open(name, header):
    path = os.path.join(OUT, name)
    new = not os.path.exists(path)
    f = io.open(path, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new: w.writerow(header)
    return f, w

def cycle(p, mv=5000, off=5.0):
    """電源を入れ直し、ポートが戻るまで待つ。"""
    p.apply(False, mv, 1000); time.sleep(off)
    p.apply(True, mv, 1000);  time.sleep(3.0)
    dl = time.time() + 25
    while time.time() < dl and PORT not in [q.device for q in lp.comports()]:
        time.sleep(0.3)
    time.sleep(2.0)

def do_wake(banks):
    alive = []
    for b in banks:
        r = burst(PORT, b, 1, T, True, lambda m: None)
        if r is not None and len(collections.Counter(r)) > 50:
            alive.append("$%02X" % b)
    return alive

def probe():
    """$C0 を1バンク読み、正解の先頭64KBと一致するか。"""
    r = burst(PORT, 0xC0, 1, T, True, lambda m: None)
    if r is None: return 0, 0
    return len(collections.Counter(r)), int(r == REF_HEAD)

# ---------------------------------------------------------------- 各段
def phase1_wake(p):
    pats = collections.OrderedDict([
        ("none", []), ("sanni", [0xC0]), ("c0x6", [0xC0]*6),
        ("e0only", [0xE0]), ("full", FULL_WAKE)])
    f, w = csv_open("wake_sweep.csv",
                    ["iso","pattern","trial","uniq","match","i_mA","alive"])
    try:
        for name, banks in pats.items():
            good = 0
            for t in range(1, 21):
                cycle(p); alive = do_wake(banks); u, m = probe()
                good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), name, t, u, m,
                            p.status()["iout_mA"], " ".join(alive)]); f.flush()
                log("  %-7s %2d %s 種類%4d %s" % (name, t, "○" if m else "×", u, " ".join(alive)))
            log("=== %-7s %d/20 ===" % (name, good))
    finally: f.close()

def phase2_voltage(p):
    f, w = csv_open("sa1_voltage.csv",
                    ["iso","v_set_mV","v_meas_mV","trial","uniq","match","i_mA"])
    try:
        for mv in (5000, 4900, 4800, 4700, 4600, 4500):
            good = 0
            for t in range(1, 21):
                cycle(p, mv); do_wake(FULL_WAKE); u, m = probe()
                s = p.status(); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, s["vout_mV"], t,
                            u, m, s["iout_mA"]]); f.flush()
                log("  %.2fV %2d %s 種類%4d" % (mv/1000, t, "○" if m else "×", u))
            log("=== %.2fV %d/20 ===" % (mv/1000, good))
    finally: f.close()

def phase3_decay(p):
    """起こしてから待って読む。窓に寿命があるか。"""
    f, w = csv_open("wake_decay.csv",
                    ["iso","delay_s","trial","uniq","match","i_mA"])
    try:
        for d in (0, 10, 30, 60, 180):
            good = 0
            for t in range(1, 16):
                cycle(p); do_wake(FULL_WAKE)
                if d: time.sleep(d)
                u, m = probe(); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), d, t, u, m,
                            p.status()["iout_mA"]]); f.flush()
                log("  待機%3ds %2d %s 種類%4d" % (d, t, "○" if m else "×", u))
            log("=== 待機%3ds %d/15 ===" % (d, good))
    finally: f.close()

def phase4_proxy(p):
    """1バンク判定と、全64バンク読みの結果が一致するかを確かめる。

    今までの実験は全部「$C0を1バンク読んで一致するか」で判定している。
    **これが本当に「4MBが正しく吸える」と同じ意味なのか、確かめていない。**
    """
    f, w = csv_open("proxy_check.csv",
                    ["iso","trial","probe_match","full_match","good_banks","crc"])
    try:
        agree = 0
        for t in range(1, 21):
            cycle(p); do_wake(FULL_WAKE)
            u, pm = probe()
            rom = burst(PORT, 0xC0, 64, T, True, lambda m: None)
            if rom is None:
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), t, pm, "", "", ""]); f.flush()
                log("  %2d 判定%s / 全64バンク読み出し失敗" % (t, "○" if pm else "×")); continue
            crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
            fm = int(crc == REF_CRC)
            gb = sum(1 for i in range(64)
                     if rom_likeness(rom[i*BANK_SIZE:(i+1)*BANK_SIZE])[0])
            agree += int(pm == fm)
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), t, pm, fm, gb, crc]); f.flush()
            log("  %2d 判定%s 全体%s バンク%d/64 CRC %s"
                % (t, "○" if pm else "×", "○" if fm else "×", gb, crc))
        log("=== 判定と全体が一致 %d/20 ===" % agree)
    finally: f.close()

def phase5_baseline(p):
    f, w = csv_open("baseline40.csv", ["iso","trial","uniq","match","i_mA","alive"])
    try:
        good = 0
        for t in range(1, 41):
            cycle(p); alive = do_wake(FULL_WAKE); u, m = probe(); good += m
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), t, u, m,
                        p.status()["iout_mA"], " ".join(alive)]); f.flush()
            log("  基準 %2d %s 種類%4d %s" % (t, "○" if m else "×", u, " ".join(alive)))
        log("=== 基準 %d/40 ===" % good)
    finally: f.close()

def main():
    u = serial.Serial(UNO, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    u.write(b"z"); time.sleep(0.3)          # クロックはハイインピーダンス固定
    log("Uno: " + u.readline().decode("ascii", "replace").strip())
    p = DP100()
    phases = [("1 ダミー読みの要素分解", phase1_wake),
              ("2 最良手順での電圧掃引", phase2_voltage),
              ("3 起動読みの持続時間",   phase3_decay),
              ("4 1バンク判定の妥当性",  phase4_proxy),
              ("5 基準条件の追試",       phase5_baseline)]
    for name, fn in phases:
        log("########## %s ##########" % name)
        t0 = time.time()
        try:
            fn(p)
        except Exception as e:
            log("!! %s で例外: %s: %s" % (name, type(e).__name__, e))
        log("########## %s 終了 (%.0f分) ##########" % (name, (time.time()-t0)/60))
    try:
        p.apply(True, 5000, 1000)           # 朝すぐ続けられるように5Vで残す
        log("電源を5.00Vに戻しました")
    finally:
        p.close(); u.close()
    log("すべて終了")

if __name__ == "__main__":
    main()
