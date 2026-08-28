"""SA-1搭載カート（スーパーマリオRPG / 星のカービィ スーパーデラックス）を吸う。

■ 要点は「クロックを一切与えないこと」
SA-1はROMとバスの間に立つバス調停チップだが、**クロックを与えなければ眠っていて、
ROMは素通しで読める**。Super FXで到達した「クロックが無いから通せないのではなく、
クロックが無いから大人しかった」が、そのままSA-1にも当てはまる。

したがって以下は**すべて不要**である:

    ・マスタークロック 21.477MHz（カート1番）  … 与えるとSA-1が起きてバスを握る
    ・CIC認証（snesCIC / PIC12F629 / 実チップ）
    ・Si5351相当のクロック源

sanniのOSCRがSA-1にsnesCICを要求するのは、OSCRが21.477MHzを供給する設計だから。
クロックを与えればSA-1は起き、起きたSA-1は認証の成立を待つ。
**設計選択が、その設計自身に要件を作っていた。**

■ 代わりに必要なのは「速く読み切ること」
SA-1は時間とともに提示内容を変える。バンクごとに接続し直す方式では
1回の吸い出しに5〜7分かかり、その間に状態が変わってしまう。
**1回の接続で全64バンクを読み切る**（nano2_master の連続バンクモード）と95秒で済む。

■ それでも毎回は開かない
窓は確率的で、実測では6回中5回。開かなかった回は「異なるバイト値が数種しかない」
一色のデータになるので、rom_likeness で弾いて次の回に賭ける。
**1回の失敗で「読めない」と結論してはいけない。**

■ 使い方
    python host/dump_sa1.py --port COM12 --out KirbySDX.sfc
    python host/dump_sa1.py --port COM12 --out SMRPG.sfc --relay COM17 --rounds 8

--relay を渡すと、リトライのたびに電源リレーで入れ直す（無人で回せる）。
渡さなければ1回読んで終わる。
"""

import argparse
import collections
import os
import sys
import time
import zlib

import serial
import serial.tools.list_ports as list_ports

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bankio import BAUD, BANK_SIZE  # noqa: E402

# No-Intro。カービィSDXは3リビジョンあるので全部受ける。
KNOWN = {
    "151bd470": "Hoshi no Kirby Super Deluxe (Japan)",
    "dbbcd010": "Hoshi no Kirby Super Deluxe (Japan) (Rev 1)",
    "1f35f230": "Hoshi no Kirby Super Deluxe (Japan) (Rev 2)",
    "5527071e": "Super Mario RPG (Japan)",
}


def rom_likeness(data):
    """本物のROMらしいかを返す。

    **「0xFFかどうか」で判定してはいけない。** 施錠状態は0xFF一色だけでなく
    0x00・0x01・0x02・0x33/0xc2の交互など、いろいろな見え方をする。
    この判定を値で書いていたせいで3回誤認した。

    本物のROMは機械語もグラフィックも圧縮データも、64KBの中に
    数十〜200種類以上の異なるバイト値が現れる。種類数で見れば値によらず弾ける。
    """
    c = collections.Counter(data)
    uniq = len(c)
    top_val, top_n = c.most_common(1)[0]
    top = top_n / len(data)
    if uniq < 50:
        return False, f"異なる値が{uniq}種しかない(最頻 0x{top_val:02x})"
    if top > 0.5:
        return False, f"0x{top_val:02x} が{top:.1%}を占める"
    return True, f"{uniq}種 / 最頻 0x{top_val:02x} {top:.1%}"


def header_at(data, off):
    if len(data) < off + 32:
        return None
    h = data[off:off + 32]
    comp = h[28] | (h[29] << 8)
    csum = h[30] | (h[31] << 8)
    if ((csum + comp) & 0xFFFF) == 0xFFFF and csum:
        return h[:21].decode("shift_jis", "replace").strip(), csum
    return None


def power_cycle(relay_port, off_s, log):
    """リレーで電源を入れ直し、リーダーのポートが戻るまで待つ。"""
    try:
        r = serial.Serial(relay_port, 115200, timeout=3)
    except Exception as e:
        log(f"  リレーを開けません: {e}")
        return False
    try:
        time.sleep(2.2)
        r.readline()
        r.write(b"1")
        time.sleep(0.3)
        r.readline()
        time.sleep(off_s)
        r.write(b"0")
        time.sleep(0.3)
        r.readline()
    finally:
        r.close()
    return True


def wait_port(port, timeout_s, log):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if port in [p.device for p in list_ports.comports()]:
            time.sleep(2.0)          # 列挙直後は開けないことがある
            return True
        time.sleep(0.5)
    log(f"  {port} が復帰しませんでした")
    return False


def burst(port, start_bank, count, timing, prime, log):
    """1回の接続で count バンクを続けて読む。ここが速度の肝。"""
    rd_us, addr_us, pulse_us = timing
    try:
        ser = serial.Serial(port, BAUD, timeout=60)
    except Exception as e:
        log(f"  ポートを開けません: {e}")
        return None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ser.read(1) == b"R":
                break
        else:
            log("  準備完了(R)が来ませんでした")
            return None

        # bit5 = prime。sanni の getCartInfo_SNES() がヘッダを読む前に必ず行う
        # 「バンク$C0で1024バイトのダミーアクセス」に相当する。
        # bit2(カートへクロック供給)は**立てない**。立てるとSA-1が起きて読めなくなる。
        flags = 0x20 if prime else 0x00
        ser.write(bytes([
            start_bank, 0,
            rd_us & 0xFF, (rd_us >> 8) & 0xFF,
            addr_us & 0xFF, (addr_us >> 8) & 0xFF,
            pulse_us & 0xFF, (pulse_us >> 8) & 0xFF,
            flags,
            1,                       # clock_ocr（クロックを出さないので未使用）
            count & 0xFF,            # 連続して読むバンク数
        ]))
        ser.flush()

        want = BANK_SIZE * count
        buf = bytearray()
        while len(buf) < want:
            chunk = ser.read(min(8192, want - len(buf)))
            if not chunk:
                log(f"  タイムアウト ({len(buf)}/{want})")
                return None
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True, help="Nano-2のCOMポート")
    p.add_argument("--out", required=True, help="保存先(.sfc)")
    p.add_argument("--start-bank", default="0xC0",
                   help="SA-1は $C0 から。既定 0xC0")
    p.add_argument("--banks", type=int, default=64)
    p.add_argument("--relay", help="電源リレーのCOMポート。渡すとリトライごとに入れ直す")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--off-seconds", type=float, default=8.0)
    p.add_argument("--rd", type=int, default=5)
    p.add_argument("--addr", type=int, default=5)
    p.add_argument("--pulse", type=int, default=3)
    p.add_argument("--no-prime", action="store_true")
    a = p.parse_args()

    start = int(str(a.start_bank), 0)
    log = lambda m: print(f"[{time.strftime('%H:%M:%S')}] {m}", flush=True)
    log(f"タイミング {a.rd}/{a.addr}/{a.pulse} / prime={'無' if a.no_prime else '有'}"
        f" / カートへのクロック供給なし")

    for rnd in range(1, a.rounds + 1):
        if a.rounds > 1:
            log(f"=== 挑戦 {rnd}/{a.rounds} ===")
        if a.relay:
            if not power_cycle(a.relay, a.off_seconds, log):
                time.sleep(10)
                continue
            if not wait_port(a.port, 30, log):
                continue

        t0 = time.time()
        rom = burst(a.port, start, a.banks, (a.rd, a.addr, a.pulse),
                    not a.no_prime, log)
        if rom is None:
            continue
        el = time.time() - t0

        good = sum(1 for i in range(len(rom) // BANK_SIZE)
                   if rom_likeness(rom[i * BANK_SIZE:(i + 1) * BANK_SIZE])[0])
        crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
        log(f"  {el:.0f}秒 / CRC32 {crc} / ROMとして成立したバンク {good}/{a.banks}")

        if good == 0:
            ok, why = rom_likeness(rom[:BANK_SIZE])
            log(f"  施錠されています（{why}）。次の回に賭けます")
            continue

        h = header_at(rom, 0x7FC0) or header_at(rom, 0xFFC0)
        if h:
            total = sum(rom) & 0xFFFF
            mark = "  ★総和一致" if total == h[1] else ""
            log(f"  ヘッダ『{h[0]}』期待0x{h[1]:04x} 計算0x{total:04x}{mark}")

        if crc in KNOWN:
            with open(a.out, "wb") as f:
                f.write(rom)
            log(f"★ No-Intro一致『{KNOWN[crc]}』 保存: {a.out}")
            return 0

        path = a.out + f".{crc}.unverified"
        with open(path, "wb") as f:
            f.write(rom)
        log(f"  既知のCRCと一致しません。{path} に保存")

    log("読み切れませんでした。--relay を付けて --rounds を増やすと無人で粘れます")
    return 1


if __name__ == "__main__":
    sys.exit(main())
