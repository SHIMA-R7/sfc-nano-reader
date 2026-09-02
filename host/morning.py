# -*- coding: utf-8 -*-
"""朝までに回す検証4段。

■ 段1 起動読みの本体は「接続の張り直し」か「読み出し」か
昨夜のデータを見直すと、答えの半分は既に出ていた。

    none  (1接続、primeあり)      0/20
    sanni (2接続、両方primeあり)  13/20
    c0x6  (7接続)                 13/20

両方ともprimeは打っている。差は接続を張り直したかどうかだけ。
1接続→2接続で効果が出尽くし、それ以上増やしても変わらない。
そこで「開いて閉じるだけ（1バイトも読まない）」を測って切り分ける。

■ 段2 なぜ $E0 なのか
$C0=65% / $E0=90%。他のバンクを単独で測る。
$C0 だけが悪いのか、$E0 だけが良いのか。sanniが使っているのは $C0 である。

■ 段4 CICの再検証
現在の根拠は 4/4 対 1/6 で薄い。**必ず最後にCIC版へ焼き戻す。**

■ 段5 セーブデータ(BW-RAM)の探索
SRAMモード(flags 0x01)で $00 と $40 を、クロックを変えながら見る。
"""
import collections, csv, io, os, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
import serial.tools.list_ports as lp
from bankio import BANK_SIZE, BAUD, TIMING_TIERS
from dump_sa1 import burst
from dp100 import DP100

PORT, UNO, NANO3 = "COM19", "COM17", "COM8"
REF_HEAD = io.open(r"C:\SFC-Dumper\KirbySDX.sfc", "rb").read()[:BANK_SIZE]
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

def touch_port():
    """開いて準備完了(R)を待って閉じるだけ。1バイトも読み出さない。"""
    try:
        s = serial.Serial(PORT, BAUD, timeout=5)
        dl = time.time() + 5
        while time.time() < dl:
            if s.read(1) == b"R": break
        s.close(); return True
    except Exception:
        return False

def probe():
    r = burst(PORT, 0xC0, 1, T, True, lambda m: None)
    if r is None: return 0, 0
    return len(collections.Counter(r)), int(r == REF_HEAD)

def burst_flags(bank, count, flags):
    """任意のフラグで読む。dump_sa1.burst は prime しか立てられないため。"""
    rd, addr, pulse = T
    try:
        s = serial.Serial(PORT, BAUD, timeout=60)
    except Exception:
        return None
    try:
        dl = time.time() + 20
        found = False
        while time.time() < dl:
            if s.read(1) == b"R": found = True; break
        if not found: return None
        s.write(bytes([bank, 0, rd & 0xFF, rd >> 8, addr & 0xFF, addr >> 8,
                       pulse & 0xFF, pulse >> 8, flags, 1, count & 0xFF]))
        s.flush()
        want = BANK_SIZE * count; buf = bytearray()
        while len(buf) < want:
            c = s.read(min(8192, want - len(buf)))
            if not c: return None
            buf += c
        return bytes(buf)
    finally:
        s.close()

def phase1(p):
    conds = [("none", 0, False), ("touch", 0, True), ("e0read", 0xE0, False)]
    f, w = csv_open("wake_mechanism.csv", ["iso","cond","trial","uniq","match"])
    try:
        for name, bank, touch in conds:
            good = 0
            for t in range(1, 21):
                cycle(p)
                if touch: touch_port()
                elif bank: burst(PORT, bank, 1, T, True, lambda m: None)
                u, m = probe(); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), name, t, u, m]); f.flush()
                log("  %-7s %2d %s 種類%4d" % (name, t, "○" if m else "×", u))
            log("=== %-7s %d/20 ===" % (name, good))
    finally: f.close()

def phase2(p):
    f, w = csv_open("wake_bank.csv", ["iso","bank","trial","uniq","match","wake_uniq"])
    try:
        for bank in (0xC0, 0xD0, 0xE0, 0xF0, 0x80):
            good = 0
            for t in range(1, 21):
                cycle(p)
                r0 = burst(PORT, bank, 1, T, True, lambda m: None)
                wu = len(collections.Counter(r0)) if r0 else 0
                u, m = probe(); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), "0x%02X" % bank,
                            t, u, m, wu]); f.flush()
                log("  $%02X %2d %s 種類%4d (起動読み時%4d)" % (bank, t, "○" if m else "×", u, wu))
            log("=== $%02X %d/20 ===" % (bank, good))
    finally: f.close()

def flash(sketch, fqbn="arduino:avr:nano:cpu=atmega328old"):
    r = subprocess.run([CLI, "upload", "-p", NANO3, "--fqbn", fqbn, sketch],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    ok = r.returncode == 0
    log("  焼き込み %s -> %s" % (os.path.basename(sketch), "OK" if ok else "失敗"))
    if not ok: log("    " + (r.stderr or "")[-300:])
    time.sleep(3)
    return ok

def phase4(p):
    f, w = csv_open("cic_recheck.csv", ["iso","cic","trial","uniq","match"])
    try:
        for label, fw in (("off", FW_BANK), ("on", FW_CIC)):
            if not flash(fw):
                log("  焼き込みに失敗したのでこの条件は飛ばす"); continue
            good = 0
            for t in range(1, 21):
                cycle(p)
                burst(PORT, 0xE0, 1, T, True, lambda m: None)
                u, m = probe(); good += m
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), label, t, u, m]); f.flush()
                log("  CIC%-3s %2d %s 種類%4d" % (label, t, "○" if m else "×", u))
            log("=== CIC%s %d/20 ===" % (label, good))
    finally:
        f.close()
        log("  CIC版へ焼き戻す")
        flash(FW_CIC)

def phase5(p, u):
    f, w = csv_open("bwram_probe.csv",
                    ["iso","clk","bank","uniq","top_val","top_pct","i_mA"])
    try:
        for cmd, clk in ((b"z", "hi-Z"), (b"7", "1MHz"), (b"3", "2MHz"),
                         (b"1", "4MHz"), (b"0", "8MHz")):
            u.write(cmd); time.sleep(0.3); u.readline()
            for rep in range(3):
                cycle(p)
                burst(PORT, 0xE0, 1, T, True, lambda m: None)
                for bank in (0x00, 0x40):
                    r = burst_flags(bank, 1, 0x01)
                    if r is None:
                        log("  %-5s $%02X 読めず" % (clk, bank)); continue
                    c = collections.Counter(r); tv, tn = c.most_common(1)[0]
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), clk, "0x%02X" % bank,
                                len(c), "0x%02x" % tv, "%.3f" % (tn/len(r)),
                                p.status()["iout_mA"]]); f.flush()
                    log("  %-5s $%02X 種類%4d 最頻 0x%02x %.1f%%"
                        % (clk, bank, len(c), tv, 100.0*tn/len(r)))
    finally:
        f.close(); u.write(b"z")

def main():
    u = serial.Serial(UNO, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    u.write(b"z"); time.sleep(0.3); log("Uno: " + u.readline().decode("ascii","replace").strip())
    p = DP100()
    for name, fn, args in [("1 起動読みの本体は何か", phase1, (p,)),
                           ("2 バンクごとの差",       phase2, (p,)),
                           ("4 CICの再検証",          phase4, (p,)),
                           ("5 BW-RAMの探索",         phase5, (p, u))]:
        log("########## %s ##########" % name); t0 = time.time()
        try: fn(*args)
        except Exception as e: log("!! %s で例外: %s: %s" % (name, type(e).__name__, e))
        log("########## %s 終了 (%.0f分) ##########" % (name, (time.time()-t0)/60))
    try:
        p.apply(True, 5000, 1000); log("電源を5.00Vに戻しました")
    finally:
        p.close(); u.close()
    log("すべて終了")

if __name__ == "__main__":
    main()
