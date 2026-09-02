# -*- coding: utf-8 -*-
"""残った検証をまとめて回す。

■ 段A CICの決着（全64バンクで測る）
朝の検証で CICなし 13/20 / CICあり 14/20 と差が消えた。だが判定は
**先頭64KBしか見ていない。** 昨日CICなしで見た壊れ方は「64/64バンクが
ROMらしいのに中身が31.6%・96.7%違う」だった。先頭だけ正常で後半が壊れる型は、
この判定では捕まらない。**全64バンクを読んで決着させる。**

1試行で「1バンク判定」と「全64バンク」の両方を記録するので、
判定の楽観バイアスの検証データも同時に40点増える。

■ 段B prime は要るのか
**全ての読み出しで prime=True を送り続けており、一度も外して測っていない。**
起動読みが効くと分かった今、primeが独立に効いているのかは未検証。

■ 段C 電圧の影響を全ダンプで測る
1バンク判定では 5.00-4.50V に傾向がなかった。だが判定は楽観側に偏る。
**全ダンプで測っても傾向が無いのかを確かめる。**

**必ず最後にCIC版へ焼き戻す。**
"""
import collections, csv, io, os, subprocess, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, BAUD, TIMING_TIERS
from dump_sa1 import burst, rom_likeness
from dp100 import DP100

PORT, UNO, NANO3 = "COM19", "COM17", "COM8"
REF = io.open(r"C:\SFC-Dumper\KirbySDX.sfc", "rb").read()
REF_HEAD = REF[:BANK_SIZE]
REF_CRC = format(zlib.crc32(REF) & 0xFFFFFFFF, "08x")
T = TIMING_TIERS[0][:3]
OUT = r"C:\SFC-Dumper\data"
CLI = r"C:\Users\yugo\arduino-cli\arduino-cli.exe"
FW_CIC  = r"C:\SFC-CIC\nano3_cicbank_oled"
FW_BANK = r"C:\SFC-Dumper\nano3_bank"

def log(m): print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def csv_open(name, header):
    path = os.path.join(OUT, name); new = not os.path.exists(path)
    f = io.open(path, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new: w.writerow(header)
    return f, w

def cycle(p, mv=5000, off=5.0):
    p.apply(False, mv, 1000); time.sleep(off)
    p.apply(True, mv, 1000);  time.sleep(3.0)
    dl = time.time() + 25
    while time.time() < dl and PORT not in [q.device for q in lp.comports()]:
        time.sleep(0.3)
    time.sleep(2.0)

def wake(bank=0xE0, prime=True):
    burst(PORT, bank, 1, T, prime, lambda m: None)

def probe(prime=True):
    r = burst(PORT, 0xC0, 1, T, prime, lambda m: None)
    if r is None: return 0, 0
    return len(collections.Counter(r)), int(r == REF_HEAD)

def full_dump():
    """全64バンク。戻り値 (一致か, CRC, 成立バンク数, 相違バイト, 最初に崩れたバンク)"""
    rom = burst(PORT, 0xC0, 64, T, True, lambda m: None)
    if rom is None: return None
    crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
    ok = int(crc == REF_CRC)
    gb = sum(1 for i in range(64)
             if rom_likeness(rom[i*BANK_SIZE:(i+1)*BANK_SIZE])[0])
    diff = first = ""
    if not ok:
        diff = sum(1 for a, b in zip(rom, REF) if a != b)
        first = next((0xC0+i for i in range(64)
                      if rom[i*BANK_SIZE:(i+1)*BANK_SIZE] != REF[i*BANK_SIZE:(i+1)*BANK_SIZE]), "")
    return ok, crc, gb, diff, first

def flash(sketch):
    r = subprocess.run([CLI, "upload", "-p", NANO3,
                        "--fqbn", "arduino:avr:nano:cpu=atmega328old", sketch],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    log("  焼き込み %s -> %s" % (os.path.basename(sketch), "OK" if ok else "失敗"))
    if not ok: log("    " + (r.stderr or "")[-300:])
    time.sleep(3); return ok

def phaseA(p):
    f, w = csv_open("cic_fulldump.csv",
        ["iso","cic","trial","probe_match","full_match","good_banks","crc","diff_bytes","first_bad"])
    try:
        for label, fw in (("off", FW_BANK), ("on", FW_CIC)):
            if not flash(fw):
                log("  焼き込み失敗。この条件は飛ばす"); continue
            gp = gf = 0
            for t in range(1, 21):
                cycle(p); wake()
                _, pm = probe(); gp += pm
                r = full_dump()
                if r is None:
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), label, t, pm,
                                "", "", "", "", ""]); f.flush()
                    log("  CIC%-3s %2d 判定%s / 全体読み出し失敗" % (label, t, "○" if pm else "×"))
                    continue
                fm, crc, gb, diff, first = r; gf += fm
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), label, t, pm, fm,
                            gb, crc, diff, first]); f.flush()
                log("  CIC%-3s %2d 判定%s 全体%s バンク%2d/64 CRC %s %s"
                    % (label, t, "○" if pm else "×", "○" if fm else "×", gb, crc,
                       "" if fm else "相違%s/最初$%s" % (diff, first)))
            log("=== CIC%s 判定 %d/20 / 全ダンプ %d/20 ===" % (label, gp, gf))
    finally:
        f.close(); log("  CIC版へ焼き戻す"); flash(FW_CIC)

def phaseB(p):
    f, w = csv_open("prime_check.csv", ["iso","prime","trial","uniq","match"])
    try:
        for pr in (True, False):
            good = 0
            for t in range(1, 21):
                cycle(p); wake(prime=pr)
                u, m = probe(prime=pr); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), int(pr), t, u, m]); f.flush()
                log("  prime%-3s %2d %s 種類%4d" % ("有" if pr else "無", t, "○" if m else "×", u))
            log("=== prime%s %d/20 ===" % ("有" if pr else "無", good))
    finally: f.close()

def phaseC(p):
    f, w = csv_open("voltage_fulldump.csv",
        ["iso","v_set_mV","trial","full_match","good_banks","crc","diff_bytes","first_bad","i_mA"])
    try:
        for mv in (5000, 4500):
            good = 0
            for t in range(1, 16):
                cycle(p, mv); wake()
                r = full_dump()
                ma = p.status()["iout_mA"]
                if r is None:
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, t, "", "", "", "", "", ma])
                    f.flush(); log("  %.2fV %2d 読み出し失敗" % (mv/1000, t)); continue
                fm, crc, gb, diff, first = r; good += fm
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), mv, t, fm, gb, crc,
                            diff, first, ma]); f.flush()
                log("  %.2fV %2d %s バンク%2d/64 CRC %s" % (mv/1000, t, "○" if fm else "×", gb, crc))
            log("=== %.2fV 全ダンプ %d/15 ===" % (mv/1000, good))
    finally: f.close()

def main():
    u = serial.Serial(UNO, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    u.write(b"z"); time.sleep(0.3); log("Uno: " + u.readline().decode("ascii","replace").strip())
    p = DP100()
    for name, fn in [("A CICの決着（全64バンク）", phaseA),
                     ("B primeは要るのか",         phaseB),
                     ("C 電圧を全ダンプで測る",     phaseC)]:
        log("########## %s ##########" % name); t0 = time.time()
        try: fn(p)
        except Exception as e: log("!! %s で例外: %s: %s" % (name, type(e).__name__, e))
        log("########## %s 終了 (%.0f分) ##########" % (name, (time.time()-t0)/60))
    try:
        p.apply(True, 5000, 1000); log("電源を5.00Vに戻しました")
    finally:
        p.close(); u.close()
    log("すべて終了")

if __name__ == "__main__":
    main()
