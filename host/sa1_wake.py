# -*- coding: utf-8 -*-
"""SA-1カートを「起こして」から一気に読む。

■ 何が分かったか（2026-08-31）
45回連続で失敗していたが、原因は窓が確率的に開かないことではなかった。
**いきなり64バンクを読みに行くと、SA-1はROMを出さない。**
先に1バンクずつ短く読んでやると、以後すべての窓($C0/$D0/$E0/$F0)が読めるようになる。

    起こす前: $C0 は 0xFF 一色（異なる値1種）
    起こした後: $C0/$D0/$E0/$F0 すべて 256種の実データ

■ 訂正（2026-09-01）: 「sanniのダミー読みでは足りない」は誤りだった
45回の失敗を根拠にそう書いたが、**実験では $C0 を1回叩くだけで 65% 通る。**
真の失敗率が35%なら45連敗は 0.35^45 ≒ 10^-20 で、事実上ありえない。
**あの45回は、いまとは別の何かが違っていた。何かは分かっていない。**

有力な候補は接続の張り方。当時の失敗は prime（**同じ接続の中で**ダミーアクセス）で、
今回の実験は**独立した接続で1バンク読んでから、別の接続で本番を読む**。
接続の張り直し（DTRによるNanoリセット）が効いている可能性がある。**未検証。**

■ 窓は閉じない（2026-09-01）
起こしてから 0/10/30/60/180 秒待って読んでも成功率が落ちない（80/80/80/80/73%）。
「1接続で64バンクを読み切れ」という要件は、**窓の寿命が理由ではない。**

■ CIC認証も必要（2026-08-31 検証）
起動読みを見つけたあと、「CICが要るという結論も交絡では」と疑って、
Nano-3を nano3_bank（CICなし）に焼き替えて比較した。**起動読みは両条件で同一。**

    CICあり : 4/4 正解（すべて 151bd470、バイト単位で一致）
    CICなし : 1/6 正解。**壊れたデータ2回**、施錠3回

CICなしの唯一の成功は、CIC版で最後に成功した11分後だった（残留の疑いが濃い）。
時間を空けた5回では一度も正解が出ていない。

**壊れ方が危険。** CICなしで「64/64バンクがROMらしい」「ヘッダ『KIRBY SUPER DELUXE』
も読める」のに、中身が正解と31.6% / 96.7% 違う回があった。
rom_likeness だけでは合格にしてしまう。**総和検査とCRC照合を外してはいけない。**

■ 使い方
    python host/sa1_wake.py --port COM19 --out KirbySDX.sfc
    python host/sa1_wake.py --port COM19 --out KirbySDX.sfc --power-cycle
"""
import argparse, os, sys, time, zlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bankio import BANK_SIZE
from dump_sa1 import burst, rom_likeness, header_at, KNOWN

# 2026-09-01、各20回の対照実験で決めた。
#   起動読みなし   0/20 ( 0%)   ← 起動読みは本当に必要
#   [0xC0] 1回    13/20 (65%)   ← sanni相当。**効かないわけではなかった**
#   [0xC0] 6回    13/20 (65%)   ← 回数を増やしても変わらない
#   [0xE0] 1回    18/20 (90%)   ← **$E0 を1回で十分**
#   6バンク全部    18/20 (90%)
# 効いているのは回数ではなくバンクの選択。$E0 だけで足りる。
# 6バンク版を既定のまま残しているのは、他のSA-1カートで $E0 以外が
# 入口になる可能性を潰していないため。カービィSDXでしか確かめていない。
WAKE_BANKS = [0x00, 0x20, 0x40, 0x80, 0xC0, 0xE0]

def wake(port, timing, log):
    """1バンクずつ短く読んで回る。読めるかどうかは見ない。起こすのが目的。"""
    alive = []
    for b in WAKE_BANKS:
        r = burst(port, b, 1, timing, True, lambda m: None)
        if r is not None and rom_likeness(r)[0]:
            alive.append("$%02X" % b)
    log("  起動読み: 実データが出た領域 = %s" % (" ".join(alive) if alive else "なし"))
    return alive

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--out", required=True)
    a.add_argument("--start-bank", default="0xC0")
    a.add_argument("--banks", type=int, default=64)
    a.add_argument("--rounds", type=int, default=3)
    a.add_argument("--power-cycle", action="store_true", help="DP100で毎回電源を入れ直す")
    a.add_argument("--off-seconds", type=float, default=6.0)
    a.add_argument("--rd", type=int, default=5)
    a.add_argument("--addr", type=int, default=5)
    a.add_argument("--pulse", type=int, default=3)
    g = a.parse_args()
    start = int(str(g.start_bank), 0); T = (g.rd, g.addr, g.pulse)
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)

    p = None
    if g.power_cycle:
        from dp100 import DP100
        import serial.tools.list_ports as lp
        p = DP100()
    try:
        for rnd in range(1, g.rounds + 1):
            log("=== 挑戦 %d/%d ===" % (rnd, g.rounds))
            if p:
                p.apply(False, 5000, 1000); time.sleep(g.off_seconds)
                p.apply(True, 5000, 1000);  time.sleep(0.5)
                s = p.status()
                if s['vout_mV'] > 5300:
                    p.apply(False, 5000, 1000); log("  出力が高すぎるため中止"); return 1
                log("  電源 %.3f V / %.0f mA" % (s['vout_mV']/1000, s['iout_mA']))
                dl = time.time() + 30
                while time.time() < dl and g.port not in [q.device for q in lp.comports()]:
                    time.sleep(0.4)
                time.sleep(2.0)
            wake(g.port, T, log)
            t0 = time.time()
            rom = burst(g.port, start, g.banks, T, True, log)
            if rom is None: log("  読み出し失敗"); continue
            good = sum(1 for i in range(g.banks)
                       if rom_likeness(rom[i*BANK_SIZE:(i+1)*BANK_SIZE])[0])
            crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
            log("  %.0f秒 / CRC32 %s / 成立バンク %d/%d"
                % (time.time()-t0, crc, good, g.banks))
            if good == 0:
                log("  施錠（%s）" % rom_likeness(rom[:BANK_SIZE])[1]); continue
            h = header_at(rom, 0x7FC0) or header_at(rom, 0xFFC0)
            if h:
                tot = sum(rom) & 0xFFFF
                log("  ヘッダ『%s』期待0x%04x 計算0x%04x%s"
                    % (h[0], h[1], tot, "  ★一致" if tot == h[1] else ""))
            if crc in KNOWN:
                open(g.out, "wb").write(rom)
                log("★ No-Intro一致『%s』 保存: %s" % (KNOWN[crc], g.out)); return 0
            open(g.out + "." + crc + ".unverified", "wb").write(rom)
            log("  既知CRCと不一致")
        return 1
    finally:
        if p: p.close()

if __name__ == "__main__":
    sys.exit(main())
