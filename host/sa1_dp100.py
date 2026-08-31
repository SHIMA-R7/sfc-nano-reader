# -*- coding: utf-8 -*-
"""DP100で電源を入り切りしながら SA-1 を粘って吸う。

■ リレーを置き換えた理由
Unoのリレーは「入」「切」しかできず、**切っている時間を細かく変えられなかった**。
DP100ならPCから直接切れるうえ、**消費電流が読める**。
「電源を切ってから時間を置く必要がある（どこかに大きなコンデンサがある）」という
仮説を、待ち時間を振って確かめられるようになった。

■ 安全
電圧は host/dp100.py の power_on_5v() が 5.000V 固定で入れる。
このスクリプトから電圧を指定することはできない。**事故は引数から入る。**

    python host/sa1_dp100.py --port COM19 --out KirbySDX.sfc --rounds 6
"""
import argparse, csv, os, sys, threading, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bankio import BANK_SIZE
from dump_sa1 import burst, rom_likeness, header_at, KNOWN
from dp100 import DP100, power_on_5v
import serial.tools.list_ports as list_ports
import io


class PowerLog:
    """DP100 を別スレッドで一定間隔サンプリングし、CSVに落とす。

    HIDは1本の口を取り合うとフレームが混ざるので、必ずロックで直列化する。
    グラフ化はこのCSVから行う（列: t_s, phase, round, mV, mA, mW）。
    """
    def __init__(self, dp, path, period=0.5):
        self.dp, self.path, self.period = dp, path, period
        self.lock = threading.Lock()
        self.phase, self.round = "init", 0
        self._stop = threading.Event()
        new = not os.path.exists(path)
        self.f = io.open(path, "a", newline="", encoding="utf-8")
        self.w = csv.writer(self.f)
        if new: self.w.writerow(["t_s","iso","phase","round","mV","mA","mW"])
        self.t0 = time.time()
        self.th = threading.Thread(target=self._run, daemon=True); self.th.start()
    def mark(self, phase, rnd=None):
        self.phase = phase
        if rnd is not None: self.round = rnd
    def read(self):
        with self.lock:
            return self.dp.status()
    def apply(self, on, v, i):
        with self.lock:
            return self.dp.apply(on, v, i)
    def _run(self):
        while not self._stop.is_set():
            try:
                s = self.read()
                if s:
                    self.w.writerow(["%.2f"%(time.time()-self.t0),
                        time.strftime("%Y-%m-%dT%H:%M:%S"), self.phase, self.round,
                        s["vout_mV"], s["iout_mA"], s["vout_mV"]*s["iout_mA"]//1000])
                    self.f.flush()
            except Exception:
                pass
            self._stop.wait(self.period)
    def close(self):
        self._stop.set(); self.th.join(timeout=2); self.f.close()

def wait_port(port, timeout_s, log):
    dl = time.time() + timeout_s
    while time.time() < dl:
        if port in [p.device for p in list_ports.comports()]:
            time.sleep(2.0); return True
        time.sleep(0.4)
    log("  %s が復帰しません" % port); return False

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--start-bank", default="0xC0")
    a.add_argument("--banks", type=int, default=64)
    a.add_argument("--rounds", type=int, default=6)
    a.add_argument("--off-seconds", type=float, nargs="*", default=[3,8,15,30],
                   help="電源を切っている時間。順に試して繰り返す")
    a.add_argument("--rd", type=int, default=5)
    a.add_argument("--addr", type=int, default=5)
    a.add_argument("--pulse", type=int, default=3)
    a.add_argument("--power-csv", default="power_log.csv", help="電力ログの保存先")
    g = a.parse_args()
    start = int(str(g.start_bank), 0)
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    p = DP100()
    pl = PowerLog(p, g.power_csv)
    log("電力ログ: %s" % g.power_csv)
    try:
        for rnd in range(1, g.rounds + 1):
            off = g.off_seconds[(rnd - 1) % len(g.off_seconds)]
            log("=== 挑戦 %d/%d  電源断 %.0f 秒 ===" % (rnd, g.rounds, off))
            pl.mark("off", rnd)
            pl.apply(False, 5000, 1000)
            time.sleep(off)
            pl.mark("on", rnd)
            pl.apply(True, 5000, 1000)
            time.sleep(0.4)
            s = pl.read()
            if s['vout_mV'] > 5300:
                pl.apply(False, 5000, 1000)
                log("  出力 %.3f V と高すぎるため中止" % (s['vout_mV']/1000)); return 1
            log("  投入 %.3f V / %.0f mA" % (s['vout_mV']/1000, s['iout_mA']))
            if not wait_port(g.port, 30, log): continue
            pl.mark("idle", rnd)
            time.sleep(1.0)
            s2 = pl.read()
            log("  待機中 %.0f mA (%.2f W)" % (s2['iout_mA'], s2['vout_mV']*s2['iout_mA']/1e6))

            pl.mark("read", rnd)
            t0 = time.time()
            rom = burst(g.port, start, g.banks, (g.rd, g.addr, g.pulse), True, log)
            s3 = pl.read()
            pl.mark("done", rnd)
            if rom is None:
                log("  読み出し失敗"); continue
            good = sum(1 for i in range(len(rom)//BANK_SIZE)
                       if rom_likeness(rom[i*BANK_SIZE:(i+1)*BANK_SIZE])[0])
            crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
            log("  %.0f秒 / 読み出し中 %.0f mA / CRC32 %s / 成立バンク %d/%d"
                % (time.time()-t0, s3['iout_mA'], crc, good, g.banks))
            if good == 0:
                log("  施錠（%s）" % rom_likeness(rom[:BANK_SIZE])[1]); continue
            h = header_at(rom, 0x7FC0) or header_at(rom, 0xFFC0)
            if h:
                tot = sum(rom) & 0xFFFF
                log("  ヘッダ『%s』期待0x%04x 計算0x%04x%s"
                    % (h[0], h[1], tot, "  ★総和一致" if tot == h[1] else ""))
            if crc in KNOWN:
                open(g.out, "wb").write(rom)
                log("★ No-Intro一致『%s』 保存: %s" % (KNOWN[crc], g.out)); return 0
            open(g.out + "." + crc + ".unverified", "wb").write(rom)
            log("  既知CRCと不一致。%s.%s.unverified に保存" % (g.out, crc))
        log("読み切れませんでした")
        return 1
    finally:
        pl.close()
        p.close()

if __name__ == "__main__":
    sys.exit(main())
