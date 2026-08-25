"""
接触が悪くて「2回一致」が取れないカートを、回数で殴って確定させる。

■ dump_voting.py と何が違うのか
dump_voting.py は Super FX 用に書いた。GSUがバスを奪って**毎回ランダムな位置**が化ける
前提なので、外れ値の判定が「サンプル1本まるごと」単位になっている。

接触不良の壊れ方はこれとは違う。README と開発史に既に書いてある実測がそのまま設計根拠:

  ・誤りは**数十〜250バイトほどの連なり**で出る。1バイトずつ独立に散らばらない。
    (「あるビットが数十msのあいだ張り付く」形。バンク内 0x380〜0x690 付近に偏る)
  ・**数分の間に品質が12倍変動する**。ヨッシーアイランドで上位32KBの0xFFが
    15,306個 → 1,268個 と動いた。良い数分と悪い数分がある。
  ・**誤りは双方向**。「ビット落ちの一方向だからORでマージできる」は一度出した結論を
    撤回してある(コミット d0d1876)。基準に多数決値を使ったせいの錯覚だった。
    したがってこのスクリプトは**ORを使わない**。

ここから設計はこうなる:

  1. 外れ値の判定を **窓ごと(既定256バイト)** に行う。壊れた区間だけを票から外し、
     同じサンプルの正常な区間はちゃんと数える。窓幅は上の「連なりの長さ」に合わせてある。
     1本まるごと捨てる方式だと、途中で浮いただけのサンプルの正常な数万バイトまで捨てる。
  2. サンプルを **バンクを一周しながら1本ずつ** 集める(スイープ)。同じバンクを連続で
     7回読むと、その7回がまるごと「悪い数分」に入る可能性がある。そうなると誤りが
     多数派になり、票を増やしても収束しない。時間軸にばらけさせるのが目的。
  3. バイト単位で決着しない場所だけ **ビット単位の多数決** に落とす。ORではない。
     双方向の誤りでも、同じビットが同じ向きに複数サンプルで同時に化けない限り効く。
  4. **収束したバンクはもう読まない**。未決着バイトが残っているバンクにだけ票を足す。

■ 読み出しタイミング
Super FX とは逆で、接触不良では遅くしても悪化しない。既定では TIMING_TIERS を1本ごとに
巡回させ、誤りの出方を意図的にばらけさせる(同じ条件で読み続けると同じ場所が同じように
化けて、票が割れずに間違いが多数派に居座る)。

■ キャッシュは条件を変えたら捨てること
生サンプルは <出力名>.votes/ に残り、再実行時に票として再利用される。**接触の悪い状態で
取ったサンプルも、良い状態のものと同じ重みで数えられる**。挿し直し・配線変更・電源変更を
したら --fresh を付けて、それまでのサンプルを退避してから取り直すこと。
(ヨッシーアイランドで、悪条件下のキャッシュが残っていてチェックサムが4だけ合わなかった
実例がある。README「キャッシュは条件を変えたら捨てること」)

■ 使い方
    # 読みながらマージ (既定: 各バンク7本、収束しなければ最大40本まで読み足す)
    python contact_merge.py --port COM12 --banks 32 --mapping lorom --out MyGame.sfc

    # 挿し直した直後など、過去のサンプルを信用したくないとき
    python contact_merge.py --port COM12 --banks 32 --mapping lorom --out MyGame.sfc --fresh

    # もう読んである生サンプルだけでマージし直す (カートを挿さなくてよい)
    python contact_merge.py --merge-only --banks 32 --mapping lorom --out MyGame.sfc

    # 手元にある完成ダンプ同士をマージする
    python contact_merge.py --merge-files a.sfc b.sfc c.sfc --out MyGame.sfc

生サンプルの形式は dump_voting.py と同じなので、キャッシュを相互に使い回せる。
"""

import argparse
import glob
import os
import shutil
import sys
import time
import zlib

import numpy as np

from bankio import BANK_SIZE, TIMING_TIERS, _read_bank_once

HALF = BANK_SIZE // 2


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--port", help="読み出しを行う場合に指定。--merge-only なら不要")
    p.add_argument("--banks", type=int, help="読むバンク数")
    p.add_argument("--out", required=True)
    p.add_argument("--mapping", choices=["lorom", "hirom", "linear"], default="lorom",
                   help="lorom は各バンクの上位32KBだけを対象にする。下位32KBは"
                        "ミラーか未駆動で結果に使わないので、そこの化けを収束判定に含めない")
    p.add_argument("--start-bank", default="0",
                   help="読み始めるバンク番号。DSP-1搭載のHiROMカートは 0xC0")
    p.add_argument("--samples", type=int, default=7,
                   help="1バンクあたりまず何本集めるか (既定7)。票が割れないよう奇数を推奨")
    p.add_argument("--max-samples", type=int, default=40,
                   help="収束しないバンクに費やす上限本数 (既定40)")
    p.add_argument("--window", type=int, default=256,
                   help="外れ値を窓単位で判定するときの窓サイズ (既定256バイト)。"
                        "実測された誤りの連なりの長さに合わせてある。"
                        "これを大きくすると、短い連なりが窓全体の中に埋もれて検出できない")
    p.add_argument("--confidence", type=float, default=0.75,
                   help="この得票シェアに届かなかったバイトを「未決着」として扱う (既定0.75)")
    p.add_argument("--no-bitwise", action="store_true",
                   help="バイト単位で決着しなかった場所のビット単位多数決を行わない")
    p.add_argument("--tier", default="auto",
                   help="auto なら TIMING_TIERS を1本ごとに巡回。数字で固定も可 (0=高速〜4=最安全)")
    p.add_argument("--no-escalate", action="store_true",
                   help="追加取得のたびにタイミングを1段遅くするのをやめる。"
                        "Super FXのように遅くすると悪化する相手ではこれを付ける")
    p.add_argument("--no-sweep", action="store_true",
                   help="バンクを一周しながら集めるのをやめ、1バンクずつ読み切る。"
                        "品質の時間変動に弱くなるので、通常は付けない")
    p.add_argument("--fresh", action="store_true",
                   help="既存の生サンプルを <cache>.old-<時刻> へ退避してから始める。"
                        "カートを挿し直した/配線を変えたら必ず付けること")
    p.add_argument("--seed-banks", metavar="DIR",
                   help="dump_by_bank.py が確定させた bank_NNN.bin を1票として取り込む。"
                        "悪条件下で確定したバンクが混ざっている可能性があるので既定では使わない")
    p.add_argument("--guess-checksum", action="store_true",
                   help="未決着バイトの候補を入れ替えてチェックサムが合う組み合わせを探し、"
                        "<出力名>.guessed として保存する。**これは推定であって検証ではない**。"
                        "既定では組み合わせの存在を報告するだけで、書き換えない")
    p.add_argument("--merge-only", action="store_true",
                   help="カートを読まず、キャッシュ済みの生サンプルだけでマージする")
    p.add_argument("--merge-files", nargs="+", metavar="FILE",
                   help="完成ダンプ(.sfc)同士をマージする。--banks/--mapping は使わない")
    p.add_argument("--cache", default=None, help="生サンプルの保存先 (既定: <out>.votes)")
    a = p.parse_args()
    a.start_bank = int(str(a.start_bank), 0)
    if not a.merge_files:
        if a.banks is None:
            p.error("--banks を指定してください (--merge-files を使う場合を除く)")
        if not a.merge_only and not a.port:
            p.error("--port を指定してください (--merge-only なら不要)")
    return a


# ---------------------------------------------------------------- 多数決の中身

def _tally(arr, weights, lo, hi):
    """[lo,hi) について、バイト値ごとの得票数 (256, 幅) を数える。

    256 x 区間長 の配列を作るので、ROM全体を一度に渡すとメモリを食い潰す。
    呼び出し側で区間を切ること。
    """
    width = hi - lo
    t = np.zeros((256, width), dtype=np.int16)
    col = np.arange(width)
    for i in range(arr.shape[0]):
        # 1本のサンプル内では (値, 列) の組が重複しないので、素直な加算代入でよい
        t[arr[i, lo:hi], col] += weights[i, lo:hi]
    return t


def _decide(arr, weights, chunk=32768):
    """重み付き多数決。値・最多得票数・総票数を返す。"""
    length = arr.shape[1]
    value = np.empty(length, dtype=np.uint8)
    top = np.empty(length, dtype=np.int32)
    for lo in range(0, length, chunk):
        hi = min(lo + chunk, length)
        t = _tally(arr, weights, lo, hi)
        value[lo:hi] = t.argmax(axis=0).astype(np.uint8)
        top[lo:hi] = t.max(axis=0)
    return value, top, weights.sum(axis=0, dtype=np.int32)


def merge_samples(arr, window=256, confidence=0.75, bitwise=True):
    """サンプル群 (n, L) uint8 をマージする。

    戻り値は (確定データ, 得票シェア, 未決着バイトの位置, 情報dict)。

      1. 全バイト同一のサンプル(＝何も読めていない)を捨てる。
         0xFF一色の読みは安定していて「2回一致」を平気で通すので、ここで落とす。
      2. 素の多数決で仮の答えを作る。
      3. 窓ごとに、(a)仮の答えとの相違 (b)0xFFの密度 のどちらかが他サンプルから
         極端に外れている窓を、**その窓に限って** 票から外す。
      4. 残った票で多数決を取り直し、得票シェアを信頼度とする。
      5. シェアが閾値未満のバイトだけ、ビット単位の多数決で組み直す。

    (3)(a) は仮の答え自体を基準にしている以上、循環している。多数決が丸ごと間違って
    いる窓では、正しいサンプルの方が外れ値に見える。過去に一方向のビット落ちだと
    誤断したのはまさにこの循環が原因だったので、断定はしない — 相違の少ない上位3本は
    必ず残し、判定は「多数派から遠い」以上の意味を持たせていない。(b) は多数決と
    独立した指標なので、その分だけ信用できる。
    """
    n, length = arr.shape
    info = {"dropped_degenerate": 0, "excluded_windows": 0, "total_windows": 0}

    if n > 1:
        alive = ~np.all(arr == arr[:, :1], axis=1)
        if alive.any() and not alive.all():
            info["dropped_degenerate"] = int((~alive).sum())
            arr = arr[alive]
            n = arr.shape[0]

    if n == 1:
        return arr[0].tobytes(), np.ones(length), np.empty(0, dtype=np.int64), info

    weights = np.ones((n, length), dtype=np.int16)
    prov, _, _ = _decide(arr, weights)

    # --- 窓ごとの外れ値排除 -------------------------------------------------
    nwin = (length + window - 1) // window
    pad = nwin * window - length

    def per_window(mask):
        if pad:
            mask = np.concatenate([mask, np.zeros((n, pad), dtype=bool)], axis=1)
        return mask.reshape(n, nwin, window).sum(axis=2)

    def outliers(counts):
        med = np.median(counts, axis=0)
        # 中央値の3倍を超えたら「この窓は壊れている」。全員が数バイト違う程度の場所で
        # 神経質に切り落とさないよう下限を置く。
        return counts > np.maximum(med * 3, window * 0.05 + 4)

    diff_win = per_window(arr != prov)
    bad = outliers(diff_win) | outliers(per_window(arr == 0xFF))

    # 切りすぎると多数決が成立しない。相違の少ない上位3本は必ず残す。
    np.put_along_axis(bad, np.argsort(diff_win, axis=0)[:min(3, n)], False, axis=0)

    weights = np.repeat((~bad).astype(np.int16), window, axis=1)[:, :length]
    info["excluded_windows"] = int(bad.sum())
    info["total_windows"] = int(nwin * n)
    info["per_sample_excluded"] = bad.sum(axis=1).tolist()

    value, top, total = _decide(arr, weights)
    share = np.divide(top, np.maximum(total, 1), dtype=np.float64)

    # --- 決着しなかったバイトをビット単位で組み直す -------------------------
    # ORではない。誤りは双方向だと確認済みなので、ビットごとに多数決を取る。
    # 票が割れたビットが1つでもあるバイトは組み直さず、未決着のままにする。
    weak = np.where(share < confidence)[0]
    if bitwise and weak.size:
        sub = arr[:, weak].astype(np.int32)
        wsub = weights[:, weak].astype(np.int32)
        tot = wsub.sum(axis=0)
        built = np.zeros(weak.size, dtype=np.int32)
        tie = np.zeros(weak.size, dtype=bool)
        for k in range(8):
            ones = (((sub >> k) & 1) * wsub).sum(axis=0)
            built |= (ones * 2 > tot).astype(np.int32) << k
            tie |= (ones * 2 == tot)
        ok = ~tie
        value[weak[ok]] = built[ok].astype(np.uint8)
        info["bit_rebuilt"] = int(ok.sum())
        info["bit_tied"] = int(tie.sum())

    info["samples_used"] = n
    return value.tobytes(), share, weak, info


def candidates_at(arr, positions, current, limit=4):
    """未決着バイトについて、実際に観測された値を候補として集める。

    候補は「どれか1本のサンプルが実際に返した値」に限る。無い値をでっち上げないため。
    先頭は現在の確定値(ビット単位で組み直した値を含む)。
    """
    out = {}
    for p in positions:
        vals, counts = np.unique(arr[:, p], return_counts=True)
        ordered = [int(v) for v in vals[np.argsort(-counts)]][:limit]
        cur = int(current[p])
        if cur in ordered:
            ordered.remove(cur)
        out[int(p)] = [cur] + ordered
    return out


# ---------------------------------------------------------------- チェックサム

def rom_sum(data):
    """SFCのチェックサム。2の累乗でないROMのミラー分を数える。

    12MbitのROMは 8Mbit + 4Mbit で出来ていて、後半4Mbitはアドレス空間上で2回現れる。
    本体もそう数えるので、素直に全体を足すと合わない。
    """
    n = len(data)
    if n == 0:
        return 0
    half = 1 << (n.bit_length() - 1)
    if half == n:
        return sum(data)
    rest = data[half:]
    nxt = 1 << (len(rest) - 1).bit_length()   # 残りが埋めることになる幅
    return sum(data[:half]) + rom_sum(rest) * (half // nxt)


def candidate_sizes(length):
    """実体のサイズとしてありうる大きさを、大きい順に返す。

    ヘッダのROMサイズ申告は2の累乗に切り上げられるので、2MBと名乗る12Mbitのカートが
    ある(ドラゴンクエストI・IIがこれ)。読んだ長さの半分までを、
    「2の累乗」または「2の累乗＋2の累乗」の形で試す。

    **小さい順に返す。** ミラーは水増しにしかならないので、あるサイズで総和が合うなら、
    その倍のサイズでも(末尾が素直なミラーであれば)同じ値で合ってしまう。大きい順に見ると
    水増しされた方を掴む。小さい側が偶然合う確率は1/65536程度で、そちらはCRC32のDB照合が
    捕まえる。
    """
    out = []
    for a in range(length.bit_length(), 0, -1):
        base = 1 << (a - 1)
        if base > length:
            continue
        for b in range(a - 1, -1, -1):
            size = base + (1 << (b - 1) if b else 0)
            if base <= size <= length and size >= length // 2 and size not in out:
                out.append(size)
        if base not in out and base >= length // 2:
            out.append(base)
    return sorted(set(out))


def verify(rom):
    """ヘッダ位置を決め打ちせず、補数対が成立する方を採用する。

    戻り値は (ヘッダ位置, 期待値, 計算値, 実体のサイズ)。全体で合わなかった場合は、
    末尾がミラーである可能性を考えて短いサイズも試す。
    """
    for off in (0x7FC0, 0xFFC0):
        if len(rom) < off + 32:
            continue
        comp = rom[off + 28] | (rom[off + 29] << 8)
        csum = rom[off + 30] | (rom[off + 31] << 8)
        if ((csum + comp) & 0xFFFF) == 0xFFFF and csum:
            for size in candidate_sizes(len(rom)):
                if size >= off + 32 and rom_sum(rom[:size]) & 0xFFFF == csum:
                    return off, csum, csum, size
            return off, csum, rom_sum(rom) & 0xFFFF, len(rom)
    return None, None, rom_sum(rom) & 0xFFFF, len(rom)


def search_checksum_fix(rom, cand, expected, header_off):
    """未決着バイトの候補の組み合わせで、チェックサムが合うものを探す。

    **これは検証ではない。** 16bitの総和が合う組み合わせは複数あり得るので、見つかった
    からといって正しいとは限らない。このプロジェクトではチェックサムだけが「本物か
    どうか」を決めているので、それを自分で合わせにいったら判定材料を失う。既定では
    「存在する」と報告するだけで、書き戻すのは --guess-checksum を明示したときだけ。

    総和の差だけが問題なので、位置ごとに「現在値との差」を選ぶ部分和問題として、
    65536通りの剰余をDPで潰す。
    """
    pos = [p for p in sorted(cand)
           if not (header_off is not None and header_off + 28 <= p < header_off + 32)]
    if not pos or len(pos) > 64:
        return None
    if len(rom) & (len(rom) - 1):
        # 2の累乗でないROMはミラー分を2回数えるので、1バイトの寄与が位置で変わる。
        # そこまで面倒を見る価値のある機能ではないので、ここでは扱わない。
        return None
    need = (expected - (rom_sum(rom) & 0xFFFF)) & 0xFFFF
    if need == 0:
        return None

    reach = np.zeros(65536, dtype=bool)
    reach[0] = True
    steps = []
    for p in pos:
        cur = rom[p]
        deltas = [(v - cur) & 0xFFFF for v in cand[p]]
        nxt = np.zeros(65536, dtype=bool)
        pick = np.full(65536, -1, dtype=np.int8)
        for ci, d in enumerate(deltas):
            shifted = np.roll(reach, d)
            pick[shifted & ~nxt] = ci
            nxt |= shifted
        steps.append((p, deltas, pick))
        reach = nxt
    if not reach[need]:
        return None

    fixed = bytearray(rom)
    target = need
    changed = []
    for p, deltas, pick in reversed(steps):
        ci = int(pick[target])
        if ci < 0:
            return None
        d = deltas[ci]
        if d:
            fixed[p] = cand[p][ci]
            changed.append(p)
        target = (target - d) & 0xFFFF
    if target != 0:
        return None
    return bytes(fixed), sorted(changed)


# ---------------------------------------------------------------- 読み出し

def title_of(data):
    """バンクのデータからカートのタイトルを取り出す。読めなければ None。

    補数対が成立するヘッダしか採らないので、化けた読みからでっち上げることはない。
    """
    for off in (0x7FC0, 0xFFC0):
        if len(data) < off + 32:
            continue
        h = data[off:off + 32]
        comp = h[28] | (h[29] << 8)
        csum = h[30] | (h[31] << 8)
        if ((csum + comp) & 0xFFFF) == 0xFFFF and csum:
            return h[:21].decode("shift_jis", "replace").strip()
    return None


def title_of_pool(samples):
    for s in samples:
        t = title_of(s)
        if t:
            return t
    return None


def lookup_crc(crc):
    """No-Intro準拠のDB(RetroArch同梱)にCRC32で問い合わせる。

    ヘッダのチェックサムは16bitしかなく、たまたま合ってしまうことがある。DBに載っている
    CRC32と一致すれば、それは「世界中の他の吸い出しと同じもの」という遥かに強い証拠になる。
    DBが無い環境でも動くよう、見つからなければ黙って諦める。
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "gui"))
        import rdb
        paths = rdb.default_rdb_paths()
        if not paths:
            return None
        for e in rdb.load_entries(paths[0]):
            c = e.get("crc")
            c = c.hex() if isinstance(c, (bytes, bytearray)) else c
            if isinstance(c, int):
                c = format(c, "08x")
            if c == crc:
                return e["name"], e["size"]
    except Exception:
        return None
    return None


def slice_for(data, mapping):
    """マッピングに応じて、結果に使う部分だけを取り出す。

    LoROMで下位32KBを捨ててから多数決に掛けるのが要点。使いもしない場所の化けを
    「未決着」に数えると、永久に収束しないバンクが出る。
    """
    return data[HALF:] if mapping == "lorom" else data


def load_cached(cache, bank, mapping):
    out = []
    for f in sorted(glob.glob(os.path.join(cache, f"bank_{bank:03d}_s*.bin"))):
        if os.path.getsize(f) == BANK_SIZE:
            with open(f, "rb") as fh:
                out.append(slice_for(fh.read(), mapping))
    return out


def read_one(port, bank, cache, mapping, total_banks, tier_mode, log, bump=0, save=True):
    """1本だけ読んでキャッシュに保存する。読めなければ None。

    bump は「この本は何段階遅くして読むか」。票を増やしても決着しないバンクに対して、
    追加取得のたびに1段ずつ上げるために使う。誤りの原因が接触なら遅くしても変わらないが、
    タイミングマージン不足なら劇的に減る。どちらなのかは事前には分からないので、
    **票で駄目なら微秒を払う** という順序で両方に賭ける。
    (Super FX は逆に、遅くすると悪化するので dump_voting.py は最速固定にしてある)
    """
    idx = len(glob.glob(os.path.join(cache, f"bank_{bank:03d}_s*.bin")))
    base = idx if tier_mode == "auto" else int(tier_mode)
    tier = TIMING_TIERS[min(base + bump, len(TIMING_TIERS) - 1)
                        if tier_mode != "auto" else (base + bump) % len(TIMING_TIERS)]
    data = _read_bank_once(port, bank, tier, total_banks=total_banks, log=log)
    if data is None:
        log(f"    [{tier[3]}] 読み出し失敗")
        return None
    if all(b == data[0] for b in data):
        # 0xFF一色/0x00一色は「読めなかった」の顔。安定しているので一致判定では
        # 捕まらない。票に入れると本物より多数派になりうるので、ここで捨てる。
        log(f"    [{tier[3]}] 全バイトが 0x{data[0]:02x}。カートが浮いています。破棄")
        return None
    if save:
        # save=False はカートの照合用。まだ「同じカートか」が分かっていない読みを
        # キャッシュに書くと、取り違えを検出して中断しても別カートのサンプルが
        # ファイルとして残り、次回それが票として読み込まれてしまう。
        with open(os.path.join(cache, f"bank_{bank:03d}_s{idx:02d}.bin"), "wb") as fh:
            fh.write(data)
    return slice_for(data, mapping)


def stack(samples):
    return np.stack([np.frombuffer(s, dtype=np.uint8) for s in samples])


# ---------------------------------------------------------------- 本体

def write_report(path, banks_info, chunk, guessed, rom):
    with open(path, "w", encoding="utf-8") as f:
        f.write("未決着バイト (ROM内オフセット / バンク / バンク内オフセット / 得票シェア)\n")
        for i, (bank_no, unresolved, share) in enumerate(banks_info):
            for p in unresolved:
                f.write(f"0x{i * chunk + int(p):08X}  bank ${bank_no:02X}  "
                        f"+0x{int(p):04X}  {share[p]:.2f}\n")
        if guessed:
            f.write("\nチェックサムを合わせるために推定で書き換えた位置 (.guessed):\n")
            for p in guessed:
                f.write(f"0x{p:08X} -> 0x{rom[p]:02X}\n")


def merge_files_mode(args):
    arrs = []
    for f in args.merge_files:
        with open(f, "rb") as fh:
            arrs.append(np.frombuffer(fh.read(), dtype=np.uint8))
    size = min(a.size for a in arrs)
    if len({a.size for a in arrs}) > 1:
        print(f"サイズが揃っていません。先頭 {size} バイトだけを対象にします。", flush=True)
    arr = np.stack([a[:size] for a in arrs])
    data, share, unresolved, info = merge_samples(
        arr, args.window, args.confidence, not args.no_bitwise)
    print(f"{len(arrs)}本をマージ → 未決着 {unresolved.size} バイト / "
          f"窓の除外 {info['excluded_windows']}/{info['total_windows']}", flush=True)
    cand = candidates_at(arr, unresolved, np.frombuffer(data, dtype=np.uint8)) \
        if unresolved.size else {}
    return data, [(0, unresolved, share)], size, cand


def dump_mode(args):
    cache = args.cache or (args.out + ".votes")
    if args.fresh and os.path.isdir(cache):
        old = f"{cache}.old-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.move(cache, old)
        print(f"既存のサンプルを退避しました: {old}", flush=True)
    os.makedirs(cache, exist_ok=True)

    chunk = HALF if args.mapping == "lorom" else BANK_SIZE
    log = lambda m: print(m, flush=True)
    order = [args.start_bank + i for i in range(args.banks)]

    pools = {}
    for b in order:
        samples = load_cached(cache, b, args.mapping)
        if args.seed_banks:
            seed = os.path.join(args.seed_banks, f"bank_{b:03d}.bin")
            if os.path.exists(seed) and os.path.getsize(seed) == BANK_SIZE:
                with open(seed, "rb") as fh:
                    samples.append(slice_for(fh.read(), args.mapping))
        pools[b] = samples
    have = sum(len(v) for v in pools.values())
    cached_title = title_of_pool(pools[args.start_bank]) if pools[args.start_bank] else None
    if have:
        print(f"既存のサンプル {have} 本を再利用します。"
              f"挿し直しなど条件を変えた後なら --fresh を付けて取り直してください。",
              flush=True)
        # キャッシュは「どのカートから取ったか」を覚えていない。出力名だけを頼りにすると、
        # 別のカートで取ったサンプルを平気で混ぜる（実際、KAMAITACHINOYORU という名前の
        # キャッシュにスターフォックスが入っていた）。分かる範囲で名乗らせておく。
        print(f"  キャッシュのカート: {cached_title or '(ヘッダを読めず不明)'}", flush=True)

    start = time.time()
    if cached_title and not args.merge_only:
        # 取り違えを何時間も走らせた後に気付くのは高い。開始バンクを1本だけ読んで
        # 名乗りを突き合わせる。読んだものは票として使うので無駄にはならない。
        print("キャッシュと同じカートかを確認しています…", flush=True)
        probe = read_one(args.port, args.start_bank, cache, args.mapping, args.banks,
                         args.tier, log, save=False)
        if probe is not None:
            now = title_of(probe)
            if now and now != cached_title:
                print(f"\nキャッシュのカートは {cached_title} ですが、"
                      f"今挿さっているのは {now} です。", flush=True)
                print("混ぜると壊れたROMができます。--fresh を付けて取り直すか、"
                      "--out の名前を変えてください。", flush=True)
                return None
            pools[args.start_bank].append(probe)
            print(f"  一致: {now or cached_title}", flush=True)

    if not args.merge_only:
        # --- 目標本数まで、バンクを一周しながら1本ずつ集める -------------------
        # 同じバンクを連続で読むと、そのサンプル群がまるごと「悪い数分」に入りうる。
        # 一周しながら集めれば、各バンクのサンプルが時間軸にばらける。
        for rnd in range(args.samples):
            todo = [b for b in order if len(pools[b]) <= rnd]
            if not todo:
                continue
            print(f"\n=== {rnd + 1}周目 ({len(todo)}バンク) ===", flush=True)
            for b in todo:
                data = read_one(args.port, b, cache, args.mapping, args.banks,
                                args.tier, log)
                if data is not None:
                    if b == args.start_bank and cached_title:
                        now = title_of(data)
                        if now and now != cached_title:
                            print(f"\nキャッシュのカートは {cached_title} ですが、"
                                  f"今挿さっているのは {now} です。", flush=True)
                            print("混ぜると壊れたROMができます。--fresh を付けて"
                                  "取り直すか、--out の名前を変えてください。", flush=True)
                            return None
                        cached_title = cached_title or now
                    pools[b].append(data)
                if args.no_sweep:
                    while len(pools[b]) < args.samples:
                        data = read_one(args.port, b, cache, args.mapping, args.banks,
                                        args.tier, log)
                        if data is None:
                            break
                        pools[b].append(data)
            if args.no_sweep:
                break

    empty = [b for b in order if not pools[b]]
    if empty:
        print(f"サンプルが1本も無いバンクがあります: "
              f"{', '.join(f'${b:02X}' for b in empty)}", flush=True)
        return None

    results = {}
    pending = []
    for b in order:
        data, share, unresolved, info = merge_samples(
            stack(pools[b]), args.window, args.confidence, not args.no_bitwise)
        results[b] = (data, share, unresolved)
        print(f"bank ${b:02X}: {len(pools[b])}本 → 未決着 {unresolved.size} バイト / "
              f"窓の除外 {info['excluded_windows']}/{info['total_windows']}"
              + (f" / ビット再構成 {info['bit_rebuilt']}" if "bit_rebuilt" in info else ""),
              flush=True)
        if unresolved.size:
            pending.append(b)

    # --- 収束していないバンクにだけ、一周ずつ票を足す -----------------------
    # 読み出しに失敗しただけのバンクを対象から外してしまうと、未決着を抱えたまま
    # 黙って打ち切ることになる。連続で失敗した回数を数えて、そのときだけ諦める。
    fails = {b: 0 for b in order}
    bump = 0
    while pending and not args.merge_only:
        bump = 0 if args.no_escalate else bump + 1
        pending = [b for b in pending if len(pools[b]) < args.max_samples]
        if not pending:
            break
        tier_note = ""
        if not args.no_escalate and args.tier != "auto":
            idx = min(int(args.tier) + bump, len(TIMING_TIERS) - 1)
            tier_note = f" / タイミングを [{TIMING_TIERS[idx][3]}] に上げて読む"
        print(f"\n=== 追加取得: 未決着の残るバンク {len(pending)} 本{tier_note} ===",
              flush=True)
        still = []
        for b in pending:
            data = read_one(args.port, b, cache, args.mapping, args.banks, args.tier,
                            log, bump=bump)
            if data is None:
                fails[b] += 1
                if fails[b] >= 3:
                    print(f"bank ${b:02X}: 3回続けて読めませんでした。このバンクは打ち切り "
                          f"(未決着 {results[b][2].size} バイトが残ります)", flush=True)
                else:
                    still.append(b)
                continue
            fails[b] = 0
            pools[b].append(data)
            data, share, unresolved, info = merge_samples(
                stack(pools[b]), args.window, args.confidence, not args.no_bitwise)
            results[b] = (data, share, unresolved)
            print(f"bank ${b:02X}: {len(pools[b])}本 → 未決着 {unresolved.size} バイト",
                  flush=True)
            if unresolved.size:
                still.append(b)
        if len(still) == len(pending):
            worst = max(still, key=lambda b: results[b][2].size)
            if results[worst][2].size > chunk // 100:
                print("  未決着が減っていません。カートを挿し直して --fresh で"
                      "取り直した方が速い可能性があります。", flush=True)
        pending = still

    print(f"\n所要 {time.time() - start:.0f}秒", flush=True)

    rom = b"".join(results[b][0] for b in order)
    banks_info = [(b, results[b][2], results[b][1]) for b in order]
    cand = {}
    for i, b in enumerate(order):
        un = results[b][2]
        if un.size:
            cur = np.frombuffer(results[b][0], dtype=np.uint8)
            for p, vals in candidates_at(stack(pools[b]), un, cur).items():
                cand[i * chunk + p] = vals
    return rom, banks_info, chunk, cand


def main():
    args = parse_args()
    out = merge_files_mode(args) if args.merge_files else dump_mode(args)
    if out is None:
        return 1
    rom, banks_info, chunk, cand = out

    total_unresolved = sum(len(u) for _, u, _ in banks_info)
    off, expected, computed, size = verify(rom)
    print(f"\n合計 {len(rom)} bytes / 未決着 {total_unresolved} バイト", flush=True)
    if off is None:
        print("有効なヘッダが見つかりませんでした。--mapping の指定を確認してください。",
              flush=True)
        ok = False
    else:
        ok = computed == expected
        print(f"ヘッダ位置 0x{off:04X} / 計算値=0x{computed:04x} 期待値=0x{expected:04x}"
              f" -> {'一致' if ok else '不一致'}", flush=True)
        if ok and size != len(rom):
            # ヘッダのROMサイズ申告は2の累乗に切り上げられている。実体はこちら。
            print(f"実体は {size} bytes ({size // 1024}KB) でした。"
                  f"末尾 {len(rom) - size} bytes はミラーなので切り落とします。", flush=True)
            rom = rom[:size]
    crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
    print(f"CRC32 = {crc}", flush=True)
    known = lookup_crc(crc)
    if known:
        print(f"データベース一致: {known[0]} / {known[1]} bytes", flush=True)
        ok = True
    elif ok:
        print("  ※ ヘッダのチェックサムは合いましたが、DBに該当するCRC32はありません。"
              "改造版・未収録版の可能性もありますが、サイズの解釈を疑う価値はあります。",
              flush=True)

    dest = args.out if ok else args.out + ".unverified"
    with open(dest, "wb") as f:
        f.write(rom)
    print(f"保存: {dest}", flush=True)

    guessed = None
    if not ok and off is not None and cand:
        found = search_checksum_fix(rom, cand, expected, off)
        if found:
            fixed, guessed = found
            print(f"\n未決着バイトのうち {len(guessed)} 箇所を候補値に入れ替えると"
                  f"チェックサムが一致します。", flush=True)
            print("  ※ これは推定です。総和が合う組み合わせは複数あり得るので、"
                  "一致したことを根拠にはできません。", flush=True)
            if args.guess_checksum:
                with open(args.out + ".guessed", "wb") as f:
                    f.write(fixed)
                print(f"  保存: {args.out}.guessed "
                      f"(CRC32 = {format(zlib.crc32(fixed) & 0xFFFFFFFF, '08x')})",
                      flush=True)
                rom = fixed
            else:
                print("  書き戻すなら --guess-checksum を付けて再実行してください。",
                      flush=True)
                guessed = None

    if total_unresolved:
        rp = args.out + ".report.txt"
        write_report(rp, banks_info, chunk, guessed, rom)
        print(f"未決着バイトの一覧: {rp}", flush=True)
        print("  残っているバンクを --start-bank / --banks 1 で狙い撃ちして"
              "読み足すと収束しやすいです。", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
