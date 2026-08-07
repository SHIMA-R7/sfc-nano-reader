"""
CICの信号を簡易ロジックアナライザで観測する。

■ なぜ必要になったか
CICの認証が通らない原因を切り分けるのに analogRead() で監視していたが、1回に約100us
かかるため、CICの握手がそれより速く進んでいると「Low固定」にしか見えない。実際
「振動している/していない」以上のことが何も分からず、判定条件を三度作り直しては
失敗モード側に条件を満たされ続けた。

そこでファーム側でADCをフリーランニングモードにして約6.5us間隔で記録し、7200サンプル
(約47ms)を丸ごと持ち帰る。判定をやめて波形そのものを見る。

■ これで区別できること
    Lock側が沈黙        -> チップかモード選択(4番ピン)の問題
    Lock側だけ喋る      -> Key側に信号が届いていない
    両方喋るが噛み合わない -> クロック周波数やシード値の問題

    python cic_scope.py --port COM14 --probe data --clock-ocr 7
"""

import argparse
import sys

from bankio import BAUD, TIMING_TIERS

import serial
import time

LA_BYTES = 900
DECIM = [1]
SAMPLE_US = 6.5   # ADCプリスケーラ8での1変換あたりの実測値に基づく概算
# 1バイト=1サンプル(8bit生値)なので、900バイト=900サンプル=約5.9ms


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", required=True)
    p.add_argument("--probe", choices=["reset", "data"], default="data",
                   help="reset=A6(CIC 10番/リセット出力) data=A7(CICデータ線)")
    p.add_argument("--rst-high", action="store_true",
                   help="RSTをアクティブHighとして扱う(既定はアクティブLow)")
    p.add_argument("--clock-ocr", type=int, default=7,
                   help="カートへ供給するクロックの分周値。7=1MHz 2=2.67MHz 1=4MHz")
    p.add_argument("--decim", type=int, default=1,
                   help="何サンプルに1個記録するか。大きくすると観測窓が伸びる")
    p.add_argument("--out", default=None, help="生ビット列の保存先(任意)")
    return p.parse_args()


def capture(port, probe_data, clock_ocr, rst_high=False, decim=1):
    ser = serial.Serial(port, BAUD, timeout=20)
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if ser.read(1) == b"R":
                break
        else:
            return None

        rd, addr, pulse, _ = TIMING_TIERS[0]
        header = bytes([
            0,
            ((1 if probe_data else 0) | (2 if rst_high else 0)
             | ((decim & 0x3F) << 2)),  # bit0=対象 bit1=極性 bit2以上=間引き
            rd & 0xFF, (rd >> 8) & 0xFF,
            addr & 0xFF, (addr >> 8) & 0xFF,
            pulse & 0xFF, (pulse >> 8) & 0xFF,
            0x10 | 0x04,                 # bit4=ロジックアナライザ bit2=クロック供給
            clock_ocr & 0xFF,
        ])
        ser.write(header)
        ser.flush()

        buf = bytearray()
        while len(buf) < LA_BYTES:
            chunk = ser.read(LA_BYTES - len(buf))
            if not chunk:
                break
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def unpack(raw):
    """1バイト=1サンプルの8bit生値。閾値128で二値化する。

    二値化前の生値も呼び出し側で見られるよう、両方返す。
    「静止」が測定系のバグなのか本当に信号が無いのかは、生値を見ないと分からない。
    """
    return [1 if v > 128 else 0 for v in raw]


def describe(bits):
    """波形の要約。遷移の位置と、各区間の長さを見る。"""
    lines = []
    total = len(bits)
    high = sum(bits)
    lines.append(f"サンプル数 {total} (約{total * SAMPLE_US * DECIM[0] / 1000:.1f}ms)")
    lines.append(f"High率 {high * 100 // max(1, total)}%")

    edges = [i for i in range(1, total) if bits[i] != bits[i - 1]]
    lines.append(f"遷移回数 {len(edges)}")
    if not edges:
        lines.append("→ 完全に静止。この線では何も起きていない")
        return lines, edges

    # 最初の20個の区間長を出す。握手なら規則的なパルス列が見えるはず。
    lines.append("")
    lines.append("最初の遷移までの時間と、その後の区間長:")
    lines.append(f"  開始レベル {bits[0]} / 最初の遷移まで {edges[0] * SAMPLE_US:.0f}us")
    prev = edges[0]
    for e in edges[1:21]:
        lines.append(f"    {(e - prev) * SAMPLE_US:8.1f}us  レベル{bits[prev]}")
        prev = e
    return lines, edges


def ascii_wave(bits, width=100, span=None):
    """先頭部分をざっくり波形で描く。1文字あたり複数サンプルを畳む。"""
    span = span or len(bits)
    span = min(span, len(bits))
    step = max(1, span // width)
    top, bot = [], []
    for i in range(0, span, step):
        chunk = bits[i:i + step]
        avg = sum(chunk) / len(chunk)
        if avg > 0.75:
            top.append("_"); bot.append(" ")
        elif avg < 0.25:
            top.append(" "); bot.append("_")
        else:
            top.append("|"); bot.append("|")   # その区間内で変化している
    return "".join(top), "".join(bot)


def main():
    args = parse_args()
    raw = capture(args.port, args.probe == "data", args.clock_ocr,
                  args.rst_high, args.decim)
    if raw is None or len(raw) < LA_BYTES:
        print(f"取得失敗 ({0 if raw is None else len(raw)}/{LA_BYTES}バイト)")
        return 1

    DECIM[0] = args.decim
    bits = unpack(raw)
    lines, edges = describe(bits)
    lines.insert(0, f"生値レンジ {min(raw)}〜{max(raw)} "
                    f"({min(raw)*5/255:.2f}〜{max(raw)*5/255:.2f}V) / "
                    f"異なる値 {len(set(raw))}種")
    probe_name = "A7 CICデータ線" if args.probe == "data" else "A6 CIC 10番(リセット出力)"
    pol = "アクティブHigh" if args.rst_high else "アクティブLow"
    print(f"■ 観測対象: {probe_name} / クロック分周={args.clock_ocr} / RST={pol}")
    print("\n".join(lines))

    if edges:
        print("\n先頭5ms の波形:")
        span = int(5000 / SAMPLE_US)
        t, b = ascii_wave(bits, 100, span)
        print("  " + t)
        print("  " + b)

    if args.out:
        with open(args.out, "wb") as f:
            f.write(raw)
        print(f"\n生データ保存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
