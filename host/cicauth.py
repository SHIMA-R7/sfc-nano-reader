# -*- coding: utf-8 -*-
"""CIC認証を任意のタイミングでやり直す。

■ なぜ必要だったか
Nano-3は**電源投入から300ms後に1回だけ認証し、それきり**だった。
やり直すには電源を切るしかなく、実験のたびに電源サイクルが要る原因になっていた。

■ 仕組み（配線を増やさない）
バンクリセット線のパルス幅で指示を分ける。

    短いパルス  バンクを0に戻す（従来どおり）
    長いパルス  それに加えてCIC認証をやり直す

Nano-1に /WR を足したときと同じ手。Nano-3はPCと通信できないので、
既にある線の使い方を増やすことで合図を送る。

■ 使い方
    python host/cicauth.py --port COM19            # 認証だけやり直す
    from cicauth import reauth; reauth("COM19")    # 他のスクリプトから
"""
import argparse, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial
from bankio import BAUD, TIMING_TIERS

def reauth(port, timeout_s=20.0, log=print):
    """CIC認証をやり直させる。成功なら True。

    Nano-2 は認証の完了を待ってから 'A' を返す。**戻るまで読み出しを始めないこと。**
    """
    rd, ad, pl = TIMING_TIERS[0][:3]
    try:
        s = serial.Serial(port, BAUD, timeout=timeout_s)
    except Exception as e:
        log("  ポートを開けません: %s" % e); return False
    try:
        dl = time.time() + timeout_s
        while time.time() < dl:
            if s.read(1) == b"R": break
        else:
            log("  準備完了(R)が来ません"); return False
        # flags bit7 = 再認証。count(hdr[10])=0 なら認証だけで戻る
        s.write(bytes([0, 0, rd & 0xFF, rd >> 8, ad & 0xFF, ad >> 8,
                       pl & 0xFF, pl >> 8, 0x80, 1, 0]))
        s.flush()
        r = s.read(1)
        ok = (r == b"A")
        log("  CIC認証やり直し: %s" % ("応答あり" if ok else "応答なし(%r)" % r))
        return ok
    finally:
        s.close()

def main():
    a = argparse.ArgumentParser()
    a.add_argument("--port", required=True)
    a.add_argument("--times", type=int, default=1)
    g = a.parse_args()
    log = lambda m: print("[%s] %s" % (time.strftime("%H:%M:%S"), m), flush=True)
    ok = 0
    for i in range(1, g.times + 1):
        log("=== %d/%d ===" % (i, g.times))
        if reauth(g.port, log=log): ok += 1
        time.sleep(0.5)
    log("応答 %d/%d" % (ok, g.times))
    log("**成否はNano-3のOLEDで確認すること。** 2行目に re-auth done / FAILED が出る。")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
