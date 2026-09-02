# -*- coding: utf-8 -*-
"""マリオRPGで、カービィで確立した手順が通るかを確かめる。

■ なぜ必要か
起動読み・クロック・電圧の検証は**すべてカービィSDX 1枚**で行った。
通れば「SA-1カート一般の手順」と言えるが、通らなければ
「カービィ固有の何か」だった可能性が残る。

手順は確定版を使う。クロックはハイインピーダンス、起動読みは $E0 を1回、
prime あり、1接続で64バンク。
"""
import collections, csv, io, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial, serial.tools.list_ports as lp
from bankio import BANK_SIZE, TIMING_TIERS
from dump_sa1 import burst, rom_likeness, header_at, KNOWN
from dp100 import DP100

PORT, UNO = "COM19", "COM17"
T = TIMING_TIERS[0][:3]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 20
OUT = r"C:\SFC-Dumper\data"

def log(m): print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

def cycle(p):
    p.apply(False, 5000, 1000); time.sleep(5.0)
    p.apply(True, 5000, 1000);  time.sleep(3.0)
    dl = time.time() + 25
    while time.time() < dl and PORT not in [q.device for q in lp.comports()]:
        time.sleep(0.3)
    time.sleep(2.0)

def main():
    u = serial.Serial(UNO, 115200, timeout=2); time.sleep(2.2); u.reset_input_buffer()
    u.write(b"z"); time.sleep(0.3); log("Uno: " + u.readline().decode("ascii","replace").strip())
    p = DP100()
    path = os.path.join(OUT, "rpg_check.csv"); new = not os.path.exists(path)
    f = io.open(path, "a", newline="", encoding="utf-8"); w = csv.writer(f)
    if new: w.writerow(["iso","trial","crc","known","good_banks","title","hdr_ok","i_mA"])
    seen = collections.Counter(); good = 0
    try:
        for t in range(1, TRIALS + 1):
            cycle(p)
            burst(PORT, 0xE0, 1, T, True, lambda m: None)      # 起動読み
            rom = burst(PORT, 0xC0, 64, T, True, lambda m: None)
            time.sleep(0.3); ma = p.status()["iout_mA"]
            if rom is None:
                w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), t, "", "", "", "", "", ma])
                f.flush(); log("  %2d 読み出し失敗" % t); continue
            crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
            gb = sum(1 for i in range(64)
                     if rom_likeness(rom[i*BANK_SIZE:(i+1)*BANK_SIZE])[0])
            h = header_at(rom, 0xFFC0) or header_at(rom, 0x7FC0)
            title = h[0] if h else ""
            hdr_ok = int(bool(h) and (sum(rom) & 0xFFFF) == h[1])
            known = KNOWN.get(crc, "")
            if known:
                good += 1
                if not os.path.exists(r"C:\SFC-Dumper\SMRPG.sfc"):
                    io.open(r"C:\SFC-Dumper\SMRPG.sfc", "wb").write(rom)
                    log("    保存: SMRPG.sfc")
            seen[crc] += 1
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%S"), t, crc, known, gb,
                        title, hdr_ok, ma]); f.flush()
            log("  %2d %s CRC %s バンク%2d/64 総和%s %s"
                % (t, "○" if known else "×", crc, gb,
                   "一致" if hdr_ok else "不一致", ("『%s』" % title) if title else ""))
        log("=== 既知CRC一致 %d/%d ===" % (good, TRIALS))
        log("  出たCRCの種類: %d 種" % len(seen))
        for c, n in seen.most_common(5):
            log("    %s ×%d %s" % (c, n, KNOWN.get(c, "")))
    finally:
        f.close(); p.apply(True, 5000, 1000); p.close(); u.close()

if __name__ == "__main__":
    main()
