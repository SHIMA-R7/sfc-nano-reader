"""
バンク単位でのカート読み出し共通ロジック。GUI(sfc_dumper_gui.py)とCLI(dump_by_bank.py)の
両方から使う。

■ タイミングのエスカレーションについて
必要な待ち時間はROMチップの個体差でかなり違う。同じ設定で Super Mario Collection と
夜光虫 は5us(最速)で一発で通ったが、Street Fighter II は5usだと2回連続一致すら
得られず、100usまで上げてようやく安定した。

全カート一律で遅い設定に固定すると、マージンが要らないカートの速度を無駄に捨てることに
なる。かといって固定で速い設定にすると、マージンが必要なカートで延々と読み直す羽目になる。

そこで「まず最速のTIERで試し、指定回数失敗したら次のTIERへ上げる」方式にした。
ファームは起動のたびにPCからタイミング値を受け取るので、書き込み直しは不要。
"""

import time

import serial

BANK_SIZE = 65536
BAUD = 1000000

# (rd_settle_us, addr_settle_us, pulse_us, ラベル)
# 実測で通った値をそのまま並べている。段階を追うごとに2〜5倍程度余裕を持たせる。
TIMING_TIERS = [
    (5, 5, 3, "高速"),
    (20, 20, 5, "やや低速"),
    (50, 50, 10, "低速"),
    (100, 100, 20, "安全"),
    (300, 300, 50, "最安全"),
]

ATTEMPTS_PER_TIER = 3  # 各段階で何回失敗したら次に上げるか


def _is_degenerate(data):
    """全バイトが同一（0x00や0xFF等）かどうか。

    「2回読んで一致」だけでは、カートが外れて何も読めていない状態（常に0x00や0xFFが
    返る）を検出できない。そういう出力は自分自身と必ず一致してしまうため。
    実データがこうなる確率は天文学的に低いので、これが出たら即座に異常とみなす。
    """
    first = data[0]
    return all(b == first for b in data)


def _read_bank_once(port, bank, tier, total_banks=0, cancel_flag=None, log=None,
                    sram=False):
    """指定タイミングで1回読む。失敗したら None。

    total_banks は読み出しには使わず、OLEDに「現在/全体」を表示させるためだけに送る。
    0を渡すと従来通りバンク番号だけの表示になる（カート判定など全体数が未確定の場面用）。

    sram=True でセーブ用SRAMモード。ファーム側が /ROMSEL をアサートしなくなる。
    """
    rd_us, addr_us, pulse_us, _ = tier
    try:
        ser = serial.Serial(port, BAUD, timeout=30)
    except Exception as e:
        if log:
            log(f"    ポートを開けません: {e}")
        return None
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if cancel_flag is not None and cancel_flag.is_set():
                return None
            if ser.read(1) == b"R":
                break
        else:
            if log:
                log("    Nanoからの準備完了(R)が来ませんでした")
            return None

        header = bytes([
            bank,
            total_banks & 0xFF,
            rd_us & 0xFF, (rd_us >> 8) & 0xFF,
            addr_us & 0xFF, (addr_us >> 8) & 0xFF,
            pulse_us & 0xFF, (pulse_us >> 8) & 0xFF,
            0x01 if sram else 0x00,
        ])
        ser.write(header)
        ser.flush()

        buf = bytearray()
        while len(buf) < BANK_SIZE:
            if cancel_flag is not None and cancel_flag.is_set():
                return None
            chunk = ser.read(min(4096, BANK_SIZE - len(buf)))
            if not chunk:
                if log:
                    log(f"    タイムアウト ({len(buf)}/{BANK_SIZE})")
                return None
            buf += chunk
        return bytes(buf)
    finally:
        ser.close()


def read_bank_confirmed(port, bank, total_banks=0, start_idx=0,
                        cancel_flag=None, log=None, progress=None):
    """バンクを読み、2回連続で完全一致するまで繰り返す。

    start_idx で TIMING_TIERS の何番目から試すかを指定できる。前のバンクで分かって
    いる「このカートに必要な段階」から始めれば、毎回わざわざ通らないと分かっている
    速い段階を無駄に試さずに済む（AdaptiveTiming参照）。
    どの段階で通ったかをログに残す。戻り値は (data, tier_idx, tier_label) か
    (None, None, None)。
    """
    log = log or (lambda msg: None)

    for idx in range(start_idx, len(TIMING_TIERS)):
        tier = TIMING_TIERS[idx]
        prev = None
        for attempt in range(1, ATTEMPTS_PER_TIER + 1):
            if cancel_flag is not None and cancel_flag.is_set():
                return None, None, None
            data = _read_bank_once(port, bank, tier, total_banks, cancel_flag, log)
            if data is None:
                continue
            if progress:
                progress(len(data), BANK_SIZE)
            if prev is not None:
                diff = sum(1 for a, b in zip(prev, data) if a != b)
                if diff == 0:
                    if _is_degenerate(data):
                        log(f"    [{tier[3]}] 一致はしたが全バイトが同一値(0x{data[0]:02x})。"
                            f"カートが外れている可能性。異常として扱います")
                    else:
                        return data, idx, tier[3]
                else:
                    log(f"    [{tier[3]}] 試行{attempt}: 前回と {diff} バイト相違")
            prev = data
        log(f"    [{tier[3]}] {ATTEMPTS_PER_TIER}回で一致せず、次の段階に上げます")

    return None, None, None


class AdaptiveTiming:
    """複数バンクにまたがって「今どの段階が必要か」を記憶する。

    read_bank_confirmed を毎回 高速(tier0) から試すと、遅いカートでは
    「どうせ通らないと分かっている段階」に毎回時間を捨てることになる
    （実際、Street Fighter II で1バンクあたり40秒超かかっていた）。

    そこで基本は「前回成功した段階」から始めて無駄な足踏みを無くしつつ、
    probe_every バンクに1回だけ最速から試し、通れば速い設定に戻す。
    カートが温まって安定した、接触が改善した等で条件が良くなった場合に
    自動で速度を取り戻せるようにするため。
    """

    def __init__(self, probe_every=5):
        self.tier_idx = 0
        self.banks_since_probe = 0
        self.probe_every = probe_every

    def next_start_idx(self):
        if self.tier_idx > 0 and self.banks_since_probe >= self.probe_every:
            return 0  # 一時的に最速から試す。ダメでもread_bank_confirmedが自動で上げ直す
        return self.tier_idx

    def report(self, tier_idx, log=None):
        probed = self.tier_idx > 0 and self.banks_since_probe >= self.probe_every
        if probed and tier_idx < self.tier_idx and log:
            log(f"  高速側へ復帰: 段階を {TIMING_TIERS[self.tier_idx][3]} → "
                f"{TIMING_TIERS[tier_idx][3]} に戻しました")
        self.tier_idx = tier_idx
        self.banks_since_probe = 0 if probed else self.banks_since_probe + 1
