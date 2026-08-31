"""
SFC Cartridge Dumper GUI

Nano x3 構成のカートリッジリーダーを、ファーム書き込みからダンプ・マージまで
GUIだけで操作するためのアプリ。ブラウザ不要、Python標準のTkinterのみで動く。

    python sfc_dumper_gui.py
"""

import glob
import os
import queue
import subprocess
import sys
import threading
import time
import binascii
import zlib
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

import rdb

# bankio.py は host/ にあるものを単一のソースとして使う（gui/には複製しない）。
# frozen(exe化)時は同梱リソースとして同じ場所に配置する（後述のビルド手順を参照）。
_HOST_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "host")
if _HOST_DIR not in sys.path:
    sys.path.insert(0, _HOST_DIR)
from bankio import (BANK_SIZE, TIMING_TIERS, AdaptiveTiming,  # noqa: E402
                    read_bank_confirmed, _read_bank_once)

# 電源(DP100)とSA-1の起動読みは追加機能。hidapi/crcmod が無い環境でも
# GUI自体は動くべきなので、読み込めなければその欄だけ無効化する。
try:
    from dp100 import DP100, VMAX_MV          # noqa: E402
    from sa1_wake import wake as sa1_wake     # noqa: E402
    PSU_AVAILABLE, PSU_IMPORT_ERROR = True, None
except Exception as _e:
    DP100 = None; VMAX_MV = 5000; sa1_wake = None
    PSU_AVAILABLE, PSU_IMPORT_ERROR = False, _e

# 多数決マージは numpy を使う。入っていない環境でもGUI自体は動くべきなので、
# 読み込めなければ「多数決」方式だけを無効化して、他の機能はそのまま使えるようにする。
try:
    from contact_merge import (candidate_sizes, merge_samples,  # noqa: E402
                               rom_sum, slice_for, title_of)
    VOTE_AVAILABLE = True
except Exception as _e:  # numpy が無い等
    VOTE_IMPORT_ERROR = str(_e)
    VOTE_AVAILABLE = False

    def rom_sum(data):
        return sum(data)

    def candidate_sizes(length):
        return [length]

DEFAULT_BAUD = 1000000

# PyInstallerでexe化すると __file__ は一時展開先を指すので、
# 出力先の既定値には「exe自身の置き場所」を使う。
if getattr(sys, "frozen", False):
    PROJECT_ROOT = os.path.dirname(sys.executable)
else:
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKETCHES = {
    "Nano-1 (アドレス A0-A15)": os.path.join(PROJECT_ROOT, "nano1_addr_low"),
    "Nano-2 (マスタ / データ)": os.path.join(PROJECT_ROOT, "nano2_master"),
    "Nano-3 (バンク A16-A23)": os.path.join(PROJECT_ROOT, "nano3_bank"),
}
FQBN = "arduino:avr:nano:cpu=atmega328old"

ARDUINO_CLI_CANDIDATES = [
    r"C:\Program Files\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe",
    r"C:\Program Files (x86)\Arduino\arduino-cli.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\arduino-ide\resources\app\lib\backend\resources\arduino-cli.exe"),
    "arduino-cli",
]


def find_arduino_cli():
    for path in ARDUINO_CLI_CANDIDATES:
        if path == "arduino-cli":
            try:
                subprocess.run([path, "version"], capture_output=True, timeout=10)
                return path
            except Exception:
                continue
        if os.path.exists(path):
            return path
    return None


# ---------------------------------------------------------------- ROM 解析

def extract_rom(raw, mapping):
    """生の64KB×Nバンクから、ROM本体を取り出す。

    LoROMではROMは各バンクの $8000-$FFFF にしか現れない。カートによっては下位32KBが
    上位のミラーになる（Super Puyo Puyo, Super Mario World）が、下位が一切駆動されず
    0x00 で読めるカートもある（Super Mario Collection, 夜光虫）。
    **上位32KBを採るのが常に正しい。**
    """
    if mapping == "hirom":
        return raw
    out = bytearray()
    half = BANK_SIZE // 2
    for i in range(0, len(raw), BANK_SIZE):
        out += raw[i + half:i + BANK_SIZE]
    return bytes(out)


def read_header(rom, off):
    """指定オフセットのヘッダを読む。妥当そうなら dict、駄目なら None。"""
    if len(rom) < off + 32:
        return None
    title = rom[off:off + 21]
    map_mode = rom[off + 0x15]
    rom_size_byte = rom[off + 0x17]
    sram_size_byte = rom[off + 0x18]
    complement = rom[off + 28] | (rom[off + 29] << 8)
    checksum = rom[off + 30] | (rom[off + 31] << 8)
    if ((checksum + complement) & 0xFFFF) != 0xFFFF or checksum == 0:
        return None
    printable = sum(1 for b in title if 0x20 <= b < 0x7F)
    # SRAM容量は 1024 << n。0なら電池バックアップ無し。異常に大きい値はヘッダ誤読なので捨てる
    sram_bytes = (1024 << sram_size_byte) if 0 < sram_size_byte <= 12 else 0
    return {
        "title": title.decode("shift_jis", errors="replace").strip(),
        "map_mode": map_mode,
        "size_kb": 1 << rom_size_byte if rom_size_byte < 20 else None,
        "sram_bytes": sram_bytes,
        "checksum": checksum,
        "printable": printable,
    }


def detect_mapping(raw_bank0):
    """バンク0の生データ(64KB)から (mapping, header) を判定する。

    ヘッダの map_mode バイトだけを見ると誤りやすいので、まず下位32KBの状態で決める。
    LoROMではROMが $8000-$FFFF にしか出ないため、下位32KBは
    「上位のミラー」か「まったく駆動されず0x00」のどちらかになる。
    HiROMは64KBフルに別データが載る。
    """
    lo = raw_bank0[:BANK_SIZE // 2]
    hi = raw_bank0[BANK_SIZE // 2:]
    hdr = read_header(raw_bank0, 0xFFC0)

    if lo == hi:
        return "lorom", hdr, "下位32KBが上位のミラー"
    if not any(lo):
        return "lorom", hdr, "下位32KBが全て0x00（駆動されていない）"
    return "hirom", hdr, "下位32KBに独自データあり"


def verify_checksum(rom, mapping):
    """チェックサムを検証する。戻り値は (一致したか, 計算値, 期待値, 実体のサイズ)。

    ヘッダのROMサイズ申告は2の累乗に切り上げられているので、読んだ長さがそのまま実体とは
    限らない。ドラゴンクエストI・IIは2MBと名乗るが実体は12Mbit(1.5MB)で、後半4Mbitは
    アドレス空間に2回現れる。全体で合わなければ短いサイズも試し、合った時点でそこまでを
    実体とみなす（末尾はミラーなので切り落とす）。
    """
    off = 0xFFC0 if mapping == "hirom" else 0x7FC0
    hdr = read_header(rom, off)
    if not hdr:
        # ヘッダが読めなくても、計算値そのものは出せる。ここでNoneを返していたため
        # 表示側が f"{computed:04x}" で落ちていた（2026-08-05から在った）。
        return False, rom_sum(rom) & 0xFFFF, None, len(rom)
    for size in candidate_sizes(len(rom)):
        if size >= off + 32 and rom_sum(rom[:size]) & 0xFFFF == hdr["checksum"]:
            return True, hdr["checksum"], hdr["checksum"], size
    return False, rom_sum(rom) & 0xFFFF, hdr["checksum"], len(rom)


def majority_merge(samples):
    n = len(samples[0])
    merged = bytearray(n)
    disputed = 0
    for i in range(n):
        best, cnt = Counter(s[i] for s in samples).most_common(1)[0]
        merged[i] = best
        if cnt < len(samples):
            disputed += 1
    return bytes(merged), disputed


def or_merge(samples):
    """全サンプルのビットORを取る。

    誤り区間の一部は「本来1のビットが0に落ちる」形で出るため、ORで復元できることがある。
    ただし誤りは一方向ではなく両方向に起きるので、ORが常に正しいわけではない。
    多数決と並べて試し、チェックサムが一致した方を採用する用途。
    """
    n = len(samples[0])
    merged = bytearray(samples[0])
    for s in samples[1:]:
        for i in range(n):
            merged[i] |= s[i]
    return bytes(merged)


# ---------------------------------------------------------------- GUI

class DumperApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("SFC Cartridge Dumper — Nano x3")
        self.geometry("900x880")
        self.minsize(780, 760)

        self.log_queue = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.samples = []
        self.cli_path = find_arduino_cli()
        self.db_entries = None      # 遅延読み込み
        self.db_hits = []
        self.db_size_bytes = None   # DBで選んだタイトルの実サイズ
        self.sram_bytes = 0         # 判定で読み取ったセーブ用SRAMの容量
        self.psu = None             # DP100。使うときに開く
        self.psu_lock = threading.Lock()
        self.busy = False
        self._closing = False    # 閉じたあとに after() が走らないように

        self._build_ui()
        if PSU_AVAILABLE:
            self._psu_open()
        self._psu_poll()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.refresh_ports()
        self.after(100, self._drain_log)
        threading.Thread(target=self._load_db, daemon=True).start()

        if serial is None:
            self.log("pyserial が見つかりません。`pip install pyserial` を実行してください。")

    # -- UI 構築 -------------------------------------------------

    def _on_close(self):
        """閉じるときの後始末。

        after() のループを止めてから破棄しないと、破棄済みウィジェットに対して
        コールバックが走り "invalid command name" が出る。
        DP100のHIDハンドルも開きっぱなしにしない（**出力は切らない**。
        吸い出し中に閉じただけで給電が消えると、かえって危ない）。
        """
        self._closing = True
        try:
            if self.psu is not None:
                with self.psu_lock:
                    self.psu.close()
        except Exception:
            pass
        self.destroy()

    # -- 電源(DP100) -------------------------------------------------
    # HIDの口は1本しかない。UIの定期読みとダンプ用スレッドが同時に叩くと
    # フレームが混ざるので、必ず self.psu_lock で直列化する。

    def _psu_open(self):
        if not PSU_AVAILABLE:
            return None
        if self.psu is None:
            try:
                self.psu = DP100()
            except Exception as e:
                self.psu_status.set("見つかりません: %s" % e)
                return None
        return self.psu

    def _psu_poll(self):
        """1秒ごとに電圧・電流を読んで表示する。"""
        if self._closing:
            return
        if PSU_AVAILABLE and self.psu is not None and not self.busy:
            try:
                with self.psu_lock:
                    s = self.psu.status(); c = self.psu.setting()
                if s and c:
                    self.psu_status.set("%.3f V / %.0f mA / %.2f W   出力=%s" % (
                        s["vout_mV"] / 1000, s["iout_mA"],
                        s["vout_mV"] * s["iout_mA"] / 1e6,
                        "入" if c["state"] else "切"))
            except Exception:
                pass
        if not self._closing:
            self.after(1000, self._psu_poll)

    def psu_on(self):
        """**手順を崩さないこと。** 5Vを書く→読み戻して確認→入れる→実測を確認。"""
        p = self._psu_open()
        if p is None:
            return
        try:
            with self.psu_lock:
                c = p.apply(False, VMAX_MV, 1000)
                if not c or c["vo_set_mV"] != VMAX_MV or c["state"] != 0:
                    raise RuntimeError("5Vの設定を確認できません: %r" % (c,))
                p.apply(True, VMAX_MV, 1000)
                time.sleep(0.4)
                s = p.status()
                if s["vout_mV"] > 5300:
                    p.apply(False, VMAX_MV, 1000)
                    raise RuntimeError("出力が %.3f V と高すぎるため切りました" % (s["vout_mV"] / 1000))
            self.log("電源ON %.3f V / %.0f mA" % (s["vout_mV"] / 1000, s["iout_mA"]))
            self.after(2500, self.refresh_ports)   # Nanoが起きるのを待ってから再検索
        except Exception as e:
            messagebox.showerror("電源", str(e)); self.log("電源: %s" % e)

    def psu_off(self):
        p = self._psu_open()
        if p is None:
            return
        try:
            with self.psu_lock:
                p.apply(False, VMAX_MV, 1000)
            self.log("電源OFF")
        except Exception as e:
            messagebox.showerror("電源", str(e))

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # 接続
        conn = ttk.LabelFrame(self, text="1. 接続")
        conn.pack(fill="x", **pad)
        ttk.Label(conn, text="シリアルポート:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.port_var = tk.StringVar()
        self.port_combo = ttk.Combobox(conn, textvariable=self.port_var, width=38, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="w", pady=6)
        ttk.Button(conn, text="再検索", command=self.refresh_ports).grid(row=0, column=2, padx=6)

        # 電源（DP100）
        # リレーの代わり。5V専用のハードなので、GUIからも電圧は選ばせない。
        psu = ttk.LabelFrame(self, text="電源 (Alientek DP100) — 5V固定")
        psu.pack(fill="x", **pad)
        self.psu_status = tk.StringVar(value="未接続")
        ttk.Label(psu, textvariable=self.psu_status, width=42).grid(
            row=0, column=0, sticky="w", padx=6, pady=6)
        self.psu_on_btn = ttk.Button(psu, text="5Vを入れる", command=self.psu_on)
        self.psu_on_btn.grid(row=0, column=1, padx=4)
        self.psu_off_btn = ttk.Button(psu, text="切る", command=self.psu_off)
        self.psu_off_btn.grid(row=0, column=2, padx=4)
        ttk.Label(psu, text="※ このダンパーは5V専用です。GUIからは5V以外を出せません",
                  foreground="#666").grid(row=0, column=3, sticky="w", padx=8)
        if not PSU_AVAILABLE:
            for b in (self.psu_on_btn, self.psu_off_btn):
                b.config(state="disabled")
            self.psu_status.set("使えません（hidapi/crcmod が必要）")

        # ファーム書き込み
        fw = ttk.LabelFrame(self, text="2. ファームウェア書き込み（各Nanoを1台ずつ接続して実行）")
        fw.pack(fill="x", **pad)
        self.board_var = tk.StringVar(value=list(SKETCHES)[1])
        ttk.Combobox(fw, textvariable=self.board_var, values=list(SKETCHES),
                     width=30, state="readonly").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Button(fw, text="書き込む", command=self.start_upload).grid(row=0, column=1, padx=6)
        cli_text = self.cli_path or "arduino-cli が見つかりません"
        ttk.Label(fw, text=cli_text, foreground="#666").grid(row=0, column=2, sticky="w", padx=6)

        # データベース検索
        db = ttk.LabelFrame(self, text="3. データベースからサイズを引く（任意）")
        db.pack(fill="x", **pad)

        ttk.Label(db, text="タイトル:").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        self.query_var = tk.StringVar()
        entry = ttk.Entry(db, textvariable=self.query_var, width=40)
        entry.grid(row=0, column=1, sticky="w")
        entry.bind("<Return>", lambda _e: self.search_db())
        ttk.Button(db, text="検索", command=self.search_db).grid(row=0, column=2, padx=6)
        self.db_status = tk.StringVar(value="未読み込み")
        ttk.Label(db, textvariable=self.db_status, foreground="#666").grid(row=0, column=3, sticky="w")

        cols = ("title", "size")
        self.results = ttk.Treeview(db, columns=cols, show="headings", height=5)
        self.results.heading("title", text="タイトル")
        self.results.heading("size", text="サイズ")
        self.results.column("title", width=560, anchor="w")
        self.results.column("size", width=120, anchor="e")
        self.results.grid(row=1, column=0, columnspan=4, sticky="we", padx=6, pady=4)
        self.results.bind("<<TreeviewSelect>>", self.on_pick_db_entry)

        # ダンプ設定
        dump = ttk.LabelFrame(self, text="4. ダンプ")
        dump.pack(fill="x", **pad)

        ttk.Label(dump, text="バンク数:").grid(row=0, column=0, sticky="w", padx=6, pady=4)
        self.banks_var = tk.StringVar(value="32")
        ttk.Entry(dump, textvariable=self.banks_var, width=8).grid(row=0, column=1, sticky="w")
        ttk.Label(dump, text="(1バンク=64KB)").grid(row=0, column=2, sticky="w")

        ttk.Label(dump, text="マッピング:").grid(row=1, column=0, sticky="w", padx=6, pady=4)
        self.mapping_var = tk.StringVar(value="auto")
        for i, (val, label) in enumerate([("auto", "自動判定"), ("lorom", "LoROM"), ("hirom", "HiROM")]):
            ttk.Radiobutton(dump, text=label, value=val, variable=self.mapping_var,
                            command=self.recompute_banks).grid(row=1, column=1 + i, sticky="w")

        ttk.Label(dump, text="出力ファイル:").grid(row=2, column=0, sticky="w", padx=6, pady=4)
        self.out_var = tk.StringVar(value=os.path.join(PROJECT_ROOT, "output.sfc"))
        ttk.Entry(dump, textvariable=self.out_var, width=58).grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Button(dump, text="参照...", command=self.choose_output).grid(row=2, column=4, padx=6)

        ttk.Label(dump, text="方式:").grid(row=3, column=0, sticky="w", padx=6, pady=4)
        self.method_var = tk.StringVar(value="confirm")
        method_row = ttk.Frame(dump)
        method_row.grid(row=3, column=1, columnspan=4, sticky="w")
        ttk.Radiobutton(method_row, text="2回読んで一致するまで繰り返す（推奨・速い）",
                        value="confirm", variable=self.method_var,
                        command=self._on_method_change).pack(side="left")
        ttk.Radiobutton(method_row, text="連続読み（最速・1バンクごとにリセットしない）",
                        value="fast", variable=self.method_var,
                        command=self._on_method_change).pack(side="left", padx=10)
        self.vote_radio = ttk.Radiobutton(
            method_row, text="多数決マージ（何度読んでも一致しないカート）",
            value="vote", variable=self.method_var, command=self._on_method_change)
        self.vote_radio.pack(side="left", padx=10)
        if not VOTE_AVAILABLE:
            self.vote_radio.config(state="disabled")

        self.vote_frame = ttk.Frame(dump)
        self.vote_frame.grid(row=4, column=0, columnspan=5, sticky="w", padx=22, pady=2)
        ttk.Label(self.vote_frame, text="1バンクあたりのサンプル数:").pack(side="left")
        self.samples_var = tk.StringVar(value="5")
        ttk.Spinbox(self.vote_frame, from_=3, to=40, width=4,
                    textvariable=self.samples_var).pack(side="left", padx=4)
        ttk.Label(self.vote_frame, text="タイミング:").pack(side="left", padx=(10, 0))
        self.tier_var = tk.StringVar(value="自動巡回")
        ttk.Combobox(self.vote_frame, textvariable=self.tier_var, state="readonly", width=12,
                     values=["自動巡回"] + [t[3] for t in TIMING_TIERS]
                     ).pack(side="left", padx=4)
        self.sweep_btn = ttk.Button(self.vote_frame, text="タイミングを掃引して決める",
                                    command=self.start_sweep)
        self.sweep_btn.pack(side="left", padx=8)

        btns = ttk.Frame(dump)
        btns.grid(row=5, column=0, columnspan=5, sticky="w", padx=6, pady=6)
        self.identify_btn = ttk.Button(btns, text="カートを判定 (1バンクだけ読む)", command=self.start_identify)
        self.identify_btn.pack(side="left")
        self.dump_btn = ttk.Button(btns, text="ダンプ開始", command=self.start_dump)
        self.dump_btn.pack(side="left", padx=6)
        self.sa1_btn = ttk.Button(btns, text="SA-1カートを吸う（起動読み＋電源サイクル）",
                                  command=self.start_sa1_dump)
        self.sa1_btn.pack(side="left", padx=6)
        if not PSU_AVAILABLE:
            self.sa1_btn.config(state="disabled")
        self.cancel_btn = ttk.Button(btns, text="中止", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")

        # セーブデータ(SRAM)
        sram = ttk.LabelFrame(self, text="5. セーブデータ（バッテリーバックアップSRAM）")
        sram.pack(fill="x", **pad)
        ttk.Label(sram,
                  text="ROMは焼き直せば手に入りますが、セーブは世界に1つです。"
                       "カートの電池が切れると永久に失われます。",
                  foreground="#666").grid(row=0, column=0, columnspan=4, sticky="w", padx=6, pady=(6, 0))
        self.sram_info = tk.StringVar(value="「カートを判定」を実行すると容量が分かります")
        ttk.Label(sram, textvariable=self.sram_info).grid(row=1, column=0, columnspan=4,
                                                          sticky="w", padx=6, pady=4)
        self.sram_btn = ttk.Button(sram, text="セーブデータを吸い出す",
                                   command=self.start_sram_dump)
        self.sram_btn.grid(row=2, column=0, sticky="w", padx=6, pady=6)
        ttk.Label(sram, text="※ /WR は+5Vに直結してあるため、書き込み事故は物理的に起こりません",
                  foreground="#666").grid(row=2, column=1, columnspan=3, sticky="w", padx=6)

        # 進捗
        prog = ttk.Frame(self)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, mode="determinate")
        self.progress.pack(fill="x", side="left", expand=True)
        self.status_var = tk.StringVar(value="待機中")
        ttk.Label(prog, textvariable=self.status_var, width=26).pack(side="right", padx=6)

        # ログ
        logf = ttk.LabelFrame(self, text="ログ")
        logf.pack(fill="both", expand=True, **pad)
        self.log_text = tk.Text(logf, height=14, wrap="word", state="disabled",
                                bg="#101418", fg="#c8d0d8", insertbackground="#c8d0d8")
        self.log_text.pack(fill="both", expand=True, side="left", padx=4, pady=4)
        sb = ttk.Scrollbar(logf, command=self.log_text.yview)
        sb.pack(fill="y", side="right")
        self.log_text.config(yscrollcommand=sb.set)

        self._on_method_change()
        if not VOTE_AVAILABLE:
            self.log(f"多数決マージは使えません（numpy が読み込めません: {VOTE_IMPORT_ERROR}）。"
                     "pip install numpy で有効になります。")

    # -- ヘルパ ---------------------------------------------------

    def log(self, msg):
        self.log_queue.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                self.log_text.insert("end", msg + "\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        if not self._closing:
            self.after(100, self._drain_log)

    def refresh_ports(self):
        if serial is None:
            return
        ports = serial.tools.list_ports.comports()
        values = [f"{p.device} — {p.description}" for p in ports]
        self.port_combo["values"] = values
        if values and not self.port_var.get():
            self.port_var.set(values[0])
        self.log(f"ポート {len(values)} 個を検出しました。")

    def selected_port(self):
        raw = self.port_var.get()
        return raw.split(" — ")[0] if raw else None

    def choose_output(self):
        path = filedialog.asksaveasfilename(defaultextension=".sfc",
                                            filetypes=[("SFC ROM", "*.sfc"), ("すべて", "*.*")])
        if path:
            self.out_var.set(path)

    # -- データベース ---------------------------------------------

    def _load_db(self):
        paths = rdb.default_rdb_paths()
        if not paths:
            self.after(0, lambda: self.db_status.set("DBなし"))
            self.log("SNESデータベース(.rdb)が見つかりません。RetroArchのdatabase/rdbを確認してください。")
            return
        try:
            entries = rdb.load_entries(paths[0])
        except Exception as e:
            self.after(0, lambda: self.db_status.set("読み込み失敗"))
            self.log(f"データベースの読み込みに失敗しました: {e}")
            return
        self.db_entries = entries
        self.after(0, lambda: self.db_status.set(f"{len(entries)}件"))
        self.log(f"データベースを読み込みました: {len(entries)}件")

    def search_db(self):
        if self.db_entries is None:
            messagebox.showinfo("読み込み中", "データベースをまだ読み込み中です。少し待ってください。")
            return
        query = self.query_var.get()
        self.db_hits = rdb.search(self.db_entries, query, limit=100)
        self.results.delete(*self.results.get_children())
        for i, e in enumerate(self.db_hits):
            size = e["size"]
            label = f"{size // 1024} KB" if size else "不明"
            self.results.insert("", "end", iid=str(i), values=(e["name"], label))
        self.log(f"「{query}」の検索結果: {len(self.db_hits)}件")

    def on_pick_db_entry(self, _event=None):
        sel = self.results.selection()
        if not sel:
            return
        entry = self.db_hits[int(sel[0])]
        if not entry["size"]:
            self.log(f"『{entry['name']}』にはサイズ情報がありません。")
            return
        self.db_size_bytes = entry["size"]
        self.log(f"選択: 『{entry['name']}』 {entry['size']} bytes "
                 f"({entry['size'] // 1024} KB)")

        safe = "".join(c for c in entry["name"] if c.isalnum() or c in " _-()").strip()
        if safe:
            self.out_var.set(os.path.join(PROJECT_ROOT, safe + ".sfc"))
        self.recompute_banks()

    def recompute_banks(self):
        """DBで選んだサイズと現在のマッピング設定から、必要なバンク数を出す。"""
        if not self.db_size_bytes:
            return
        mapping = self.mapping_var.get()
        if mapping == "auto":
            self.log("マッピングが「自動判定」のため、バンク数はまだ決められません。"
                     "「カートを判定」を実行するか、LoROM/HiROMを手動で選んでください。")
            return
        per_bank = BANK_SIZE if mapping == "hirom" else BANK_SIZE // 2
        banks = self.db_size_bytes // per_bank
        self.banks_var.set(str(banks))
        self.log(f"{mapping.upper()} として計算 → バンク数 {banks} "
                 f"(Nano-2 の NUM_BANKS も同じ値にして書き込み直してください)")

    def set_busy(self, busy):
        self.busy = busy            # 電源の定期読みを止めるため（HIDの取り合いを避ける）
        state = "disabled" if busy else "normal"
        self.identify_btn.config(state=state)
        self.dump_btn.config(state=state)
        self.sram_btn.config(state=state)
        if PSU_AVAILABLE:
            self.sa1_btn.config(state=state)
        self.cancel_btn.config(state="normal" if busy else "disabled")
        if busy:
            self.sweep_btn.config(state="disabled")
        else:
            self._on_method_change()

    def _on_method_change(self):
        """多数決方式のときだけ、その設定欄を触れるようにする。"""
        state = "normal" if self.method_var.get() == "vote" else "disabled"
        for w in self.vote_frame.winfo_children():
            try:
                w.config(state=state)
            except tk.TclError:
                pass

    def cancel(self):
        self.cancel_flag.set()
        self.log("中止を要求しました。現在の処理が区切りに達したら停止します。")

    def run_worker(self, fn):
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("実行中", "別の処理が実行中です。")
            return
        self.cancel_flag.clear()
        self.set_busy(True)

        def wrapper():
            try:
                fn()
            except Exception as e:
                self.log(f"エラー: {e}")
            finally:
                self.after(0, lambda: self.set_busy(False))
                self.after(0, lambda: self.status_var.set("待機中"))

        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    # -- 実処理 ---------------------------------------------------

    def start_upload(self):
        if not self.cli_path:
            messagebox.showerror("arduino-cli なし",
                                 "arduino-cli が見つかりません。Arduino IDE をインストールしてください。")
            return
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        sketch = SKETCHES[self.board_var.get()]
        self.run_worker(lambda: self._upload(port, sketch))

    def _upload(self, port, sketch):
        self.status_var.set("コンパイル中...")
        self.log(f"コンパイル: {sketch}")
        r = subprocess.run([self.cli_path, "compile", "--fqbn", FQBN, sketch],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.log(r.stdout.strip() or r.stderr.strip())
        if r.returncode != 0:
            self.log("コンパイルに失敗しました。")
            return

        self.status_var.set("書き込み中...")
        self.log(f"書き込み: {port}")
        r = subprocess.run([self.cli_path, "upload", "-p", port, "--fqbn", FQBN, sketch],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        self.log(r.stdout.strip() or r.stderr.strip())
        if r.returncode == 0:
            self.log("書き込み成功。")
        else:
            self.log("書き込み失敗。USBを一度抜き差ししてから再試行してください。")

    def _read_bank(self, port, bank, label=""):
        """1回だけ読む（最速の設定）。カート判定などマージン不問の用途向け。"""
        if serial is None:
            self.log("pyserial がありません。")
            return None
        self.after(0, lambda: self.progress.config(maximum=BANK_SIZE, value=0))

        def progress(got, total):
            self.after(0, lambda: self.progress.config(value=got))
            self.status_var.set(f"{label}{got * 100 // total}%")

        data = _read_bank_once(port, bank, TIMING_TIERS[0],
                               cancel_flag=self.cancel_flag, log=self.log)
        if data:
            progress(len(data), BANK_SIZE)
        return data

    def _confirm_bank(self, port, bank, total_banks=0, start_idx=0):
        """バンクを確定させる。マージンが足りなければ自動でタイミングを上げる。

        戻り値は (data, tier_idx, tier_label) か (None, None, None)。
        """
        def progress(got, total):
            self.after(0, lambda: self.progress.config(maximum=total, value=got))
            self.status_var.set(f"bank{bank} {got * 100 // total}%")

        return read_bank_confirmed(port, bank, total_banks=total_banks, start_idx=start_idx,
                                   cancel_flag=self.cancel_flag,
                                   log=self.log, progress=progress)

    def start_identify(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        self.run_worker(lambda: self._identify(port))

    def _identify(self, port):
        self.log("カート判定のためバンク0を読み取ります。")
        raw = self._read_bank(port, 0, "判定用 ")
        if raw is None:
            return

        mapping, hdr, reason = detect_mapping(raw)
        self.log(f"判定結果: {mapping.upper()}  （根拠: {reason}）")
        if not hdr:
            self.log("ヘッダを認識できませんでした。カートの接触や配線を確認してください。")
            return

        self.log(f"  タイトル『{hdr['title']}』")
        self.log(f"  map_mode=0x{hdr['map_mode']:02x} / ROMサイズ申告 {hdr['size_kb']} KB "
                 f"/ チェックサム 0x{hdr['checksum']:04x}")

        self.mapping_var.set(mapping)
        self.sram_bytes = hdr["sram_bytes"]
        if self.sram_bytes:
            self.log(f"  セーブ用SRAM {self.sram_bytes // 1024} KB を検出しました。")
            self.after(0, lambda: self.sram_info.set(
                f"『{hdr['title']}』 SRAM {self.sram_bytes // 1024} KB "
                f"（{mapping.upper()}）— 吸い出せます"))
        else:
            self.log("  このカートにセーブ用SRAMはありません。")
            self.after(0, lambda: self.sram_info.set(
                f"『{hdr['title']}』 — セーブ領域なし"))

        if self.db_size_bytes:
            self.log("  データベースで選択済みのサイズを優先します。")
            self.recompute_banks()
        elif hdr["size_kb"]:
            per_bank = BANK_SIZE if mapping == "hirom" else BANK_SIZE // 2
            banks = hdr["size_kb"] * 1024 // per_bank
            self.banks_var.set(str(banks))
            self.log(f"  → バンク数を {banks} に設定しました。")

        if not self.db_size_bytes:
            safe = "".join(c for c in hdr["title"] if c.isalnum() or c in " _-").strip().replace(" ", "")
            if safe:
                self.out_var.set(os.path.join(PROJECT_ROOT, safe + ".sfc"))
            self.query_var.set(hdr["title"])
            self.after(0, self.search_db)

    # -- SA-1カート -------------------------------------------------

    def start_sa1_dump(self):
        """SA-1（マリオRPG / カービィSDX）を吸う。

        普通のダンプと手順が違う。**いきなり64バンクを読みに行くと読めない。**
        先に1バンクずつ短く読んで起こしてから、1接続で全64バンクを読み切る。
        詳しくは host/sa1_wake.py と README を参照。
        """
        port = self.selected_port()
        if not port:
            return
        out = self.out_var.get().strip()
        if not out:
            messagebox.showwarning("出力先", "保存先を選んでください。")
            return
        if self._psu_open() is None:
            messagebox.showwarning("電源", "DP100が見つかりません。")
            return
        self.run_worker(lambda: self._dump_sa1(port, out))

    def _dump_sa1(self, port, out, rounds=3):
        from dump_sa1 import burst, rom_likeness, header_at, KNOWN
        import serial.tools.list_ports as lp
        T = (5, 5, 3)
        for rnd in range(1, rounds + 1):
            if self.cancel_flag.is_set():
                self.log("中止しました。"); return
            self.log("=== 挑戦 %d/%d ===" % (rnd, rounds))
            self.status_var.set("電源を入れ直しています")
            with self.psu_lock:
                self.psu.apply(False, VMAX_MV, 1000)
                time.sleep(6.0)
                self.psu.apply(True, VMAX_MV, 1000)
                time.sleep(0.5)
                s = self.psu.status()
                if s["vout_mV"] > 5300:
                    self.psu.apply(False, VMAX_MV, 1000)
                    self.log("出力が高すぎるため中止しました。"); return
            self.log("電源 %.3f V / %.0f mA" % (s["vout_mV"] / 1000, s["iout_mA"]))
            dl = time.time() + 30
            while time.time() < dl and port not in [q.device for q in lp.comports()]:
                time.sleep(0.4)
            time.sleep(2.0)

            self.status_var.set("起動読み（カートを起こしています）")
            alive = sa1_wake(port, T, self.log)

            self.status_var.set("64バンクを一気に読んでいます（約95秒）")
            t0 = time.time()
            rom = burst(port, 0xC0, 64, T, True, self.log)
            if rom is None:
                self.log("読み出しに失敗しました。"); continue
            good = sum(1 for i in range(64)
                       if rom_likeness(rom[i * BANK_SIZE:(i + 1) * BANK_SIZE])[0])
            crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")
            self.log("%.0f秒 / CRC32 %s / 成立バンク %d/64" % (time.time() - t0, crc, good))
            if good == 0:
                self.log("施錠されています（%s）" % rom_likeness(rom[:BANK_SIZE])[1]); continue

            # **バンク数だけで合格にしてはいけない。**
            # CICが効いていないと、64/64が「ROMらしい」のに中身が3割違う回がある。
            h = header_at(rom, 0x7FC0) or header_at(rom, 0xFFC0)
            total = sum(rom) & 0xFFFF
            if h:
                self.log("ヘッダ『%s』期待0x%04x 計算0x%04x %s"
                         % (h[0], h[1], total, "★一致" if total == h[1] else "**不一致**"))
            if crc in KNOWN:
                with open(out, "wb") as f:
                    f.write(rom)
                self.log("★ No-Intro一致『%s』 保存: %s" % (KNOWN[crc], out))
                self.status_var.set("完了"); return
            if h and total == h[1]:
                with open(out, "wb") as f:
                    f.write(rom)
                self.log("総和は一致しましたが既知CRCと違います。保存: %s" % out)
                self.status_var.set("完了（未照合）"); return
            self.log("**検査に通りません。保存しません。** 次の回に賭けます")
        self.log("読み切れませんでした。CIC認証のファームが載っているか確認してください。")
        self.status_var.set("失敗")

    def start_sram_dump(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        mapping = self.mapping_var.get()
        if mapping == "auto":
            messagebox.showerror("マッピング未確定",
                                 "先に「カートを判定」を実行してください。\n"
                                 "SRAMの位置と /ROMSEL の極性がマッピングで変わるためです。")
            return
        if not self.sram_bytes:
            messagebox.showerror("SRAM容量が不明",
                                 "先に「カートを判定」を実行してください。\n"
                                 "判定してもSRAMなしと出る場合、このカートにセーブ領域はありません。")
            return
        default = os.path.splitext(self.out_var.get() or
                                   os.path.join(PROJECT_ROOT, "save"))[0] + ".srm"
        out = filedialog.asksaveasfilename(
            title="セーブデータの保存先", defaultextension=".srm",
            initialfile=os.path.basename(default), initialdir=os.path.dirname(default),
            filetypes=[("セーブデータ", "*.srm"), ("すべて", "*.*")])
        if not out:
            return
        self.run_worker(lambda: self._dump_sram(port, mapping, self.sram_bytes, out))

    def _dump_sram(self, port, mapping, size, out):
        """セーブ用SRAMを読む。

        /ROMSEL の極性はマッピングで逆になる。SNESの /CART はバンク $40-$7D では
        全アドレスでアサートされるが、$00-$3F では $8000-$FFFF のときだけ。よって
        LoROMのSRAM($70:0000)はアサートする側、HiROMのSRAM($20:6000)はしない側になる。
        """
        if mapping == "hirom":
            bank, no_romsel, window_off, window_len = 0x20, True, 0x6000, 0x2000
        else:
            bank, no_romsel, window_off, window_len = 0x70, False, 0x0000, 0x8000

        self.log(f"セーブデータを読みます（{mapping.upper()} / {size // 1024}KB / "
                 f"バンク ${bank:02X} / /ROMSEL {'非アサート' if no_romsel else 'アサート'}）")

        def progress(got, total):
            self.after(0, lambda: self.progress.config(maximum=total, value=got))
            self.status_var.set(f"SRAM {got * 100 // total}%")

        prev = None
        for attempt in range(1, 7):
            if self.cancel_flag.is_set():
                return
            raw = _read_bank_once(port, bank, TIMING_TIERS[0], cancel_flag=self.cancel_flag,
                                  log=self.log, sram=no_romsel)
            if raw is None:
                self.log(f"  試行{attempt}: 読み出せませんでした")
                continue
            progress(len(raw), len(raw))
            window = raw[window_off:window_off + window_len]
            if size > len(window):
                self.log(f"  SRAM容量({size})が窓({len(window)})より大きく、扱えません。")
                return
            body = window[:size]

            # ミラーは「1回の読み出しだけで完結する」補助的な検査。カートによっては
            # SRAM外がオープンバスになりミラーが出ないので、判定の主軸にはしない。
            mirrors = [window[i:i + size] for i in range(size, len(window) - size + 1, size)]
            bad = sum(1 for m in mirrors if m != body)
            hint = (f"ミラー{len(mirrors)}個すべて一致" if mirrors and bad == 0
                    else f"ミラー{bad}/{len(mirrors)}個が不一致" if mirrors else "ミラーなし")

            if prev is not None and prev == body:
                self.log(f"  試行{attempt}: 2回一致 / {hint}")
                if all(b == body[0] for b in body):
                    self.log(f"  ※ 全バイトが 0x{body[0]:02x} です。SRAMに届いていないか、"
                             "セーブが空の可能性があります。")
                with open(out, "wb") as f:
                    f.write(body)
                self.log(f"保存: {out} ({len(body)}バイト)")
                self.log("  RetroArchで使うには saves/<コア名>/<ROMと同じ名前>.srm に置きます。")
                self.after(0, lambda: messagebox.showinfo(
                    "セーブ吸い出し成功", f"{len(body)}バイトを保存しました。\n{out}"))
                return
            if prev is not None:
                diff = sum(1 for a, b in zip(prev, body) if a != b)
                self.log(f"  試行{attempt}: 前回と {diff} バイト相違 / {hint}")
            else:
                self.log(f"  試行{attempt}: {hint}。もう1回読んで確認します")
            prev = body

        self.log("一致を得られませんでした。接触を確認してもう一度試してください。")
        self.after(0, lambda: messagebox.showerror(
            "セーブ吸い出し失敗", "2回連続で一致する読み出しが得られませんでした。"))

    def start_dump(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        try:
            banks = int(self.banks_var.get())
            if banks < 1 or banks > 256:
                raise ValueError
        except ValueError:
            messagebox.showerror("入力エラー", "バンク数は1〜256の整数で指定してください。")
            return
        mapping = self.mapping_var.get()
        if mapping == "auto":
            messagebox.showerror("マッピング未確定",
                                 "「カートを判定」を実行するか、LoROM/HiROMを選んでください。")
            return
        out = self.out_var.get()
        if not out:
            messagebox.showerror("出力先未設定", "出力ファイルを指定してください。")
            return
        if self.method_var.get() == "vote":
            try:
                samples = int(self.samples_var.get())
                if samples < 3:
                    raise ValueError
            except ValueError:
                messagebox.showerror("入力エラー", "サンプル数は3以上で指定してください。")
                return
            self.run_worker(lambda: self._dump_vote(port, banks, mapping, out, samples))
            return
        if self.method_var.get() == "fast":
            self.run_worker(lambda: self._dump_fast(port, banks, mapping, out))
            return
        self.run_worker(lambda: self._dump(port, banks, mapping, out))

    def _dump(self, port, banks, mapping, out, start_bank=0):
        """バンク単位で読む。各バンクは2回読んで完全一致するまで繰り返す。

        確定したバンクは <出力名>.banks/ にキャッシュするので、中断しても再開できる。

        start_bank は読み始めるバンク番号。DSP-1搭載のHiROMカート(スーパーマリオカート等)は
        $00-$3F の $6000-$7FFF がDSP-1に乗っ取られていてROMが読めないため、DSP-1が居ない
        $C0 以降のミラーから読み直す必要がある。チェックサムが合わなかったときに自動で試す。
        """
        cache = out + ".banks"
        os.makedirs(cache, exist_ok=True)
        origin = f"（{mapping.upper()} / {banks}バンク"
        origin += f" / $%02X から）" % start_bank if start_bank else "）"
        self.log("バンク単位でダンプします" + origin)
        self.log(f"キャッシュ: {cache}")

        dump_start = time.time()
        read_count = 0  # 新規に読んだバンク数（キャッシュ済みは除く）
        adaptive = AdaptiveTiming()

        collected = []
        for i in range(banks):
            b = start_bank + i
            if self.cancel_flag.is_set():
                return
            path = os.path.join(cache, f"bank_{b:03d}.bin")
            if os.path.exists(path) and os.path.getsize(path) == BANK_SIZE:
                with open(path, "rb") as f:
                    collected.append(f.read())
                self.log(f"bank {b:3d}/{banks}: キャッシュ済み")
                continue

            self.log(f"bank {b:3d}/{banks}: 読み出し中")
            bank_start = time.time()
            data, tier_idx, tier = self._confirm_bank(
                port, b, total_banks=banks, start_idx=adaptive.next_start_idx())
            if data is None:
                if not self.cancel_flag.is_set():
                    self.log(f"bank {b}: 一致を得られませんでした。中断します。")
                    self.after(0, lambda bb=b: messagebox.showerror(
                        "読み出し失敗", f"bank {bb} で一致した読み出しが得られませんでした。"))
                return
            adaptive.report(tier_idx, log=self.log)
            bank_elapsed = time.time() - bank_start
            self.log(f"bank {b:3d}/{banks}: 完了 [{tier}] ({bank_elapsed:.1f}秒)")
            with open(path, "wb") as f:
                f.write(data)
            collected.append(data)
            read_count += 1

        return self._finish_dump(
            b"".join(collected), mapping, out, start_bank,
            time.time() - dump_start, read_count,
            retry=lambda sb: self._dump(port, banks, mapping, out, start_bank=sb))

    def _finish_dump(self, raw, mapping, out, start_bank,
                     dump_elapsed, read_count, retry=None):
        """読み終わったバイト列を、検証して保存する。

        バンクごと読みと連続読みで**まったく同じ検証を通す**ため共通化した。
        2か所に同じ処理を書くと、いつか片方だけ直して食い違う。
        retry は、チェックサムが合わないときに $C0 から読み直すための呼び出し。
        """

        rom = extract_rom(raw, mapping)
        ok, computed, expected, size = verify_checksum(rom, mapping)
        if ok and size != len(rom):
            self.log(f"実体は {size} bytes ({size // 1024}KB) でした。"
                     f"末尾 {len(rom) - size} bytes はミラーなので切り落とします。")
            self.log("  （ヘッダのROMサイズ申告は2の累乗に切り上げられています）")
            rom = rom[:size]
        crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")

        self.log(f"合計 {len(rom)} bytes")
        cs = ("0x%04x" % computed) if computed is not None else "算出不可"
        ex = ("0x%04x" % expected) if expected is not None else "**ヘッダが読めない**"
        self.log(f"  チェックサム 計算値={cs} 期待値={ex} → {'一致' if ok else '不一致'}")
        if expected is None:
            self.log("  ヘッダが見つかりません。マッピング(LoROM/HiROM)の指定違い、"
                     "バンク数の不足、カートの接触不良のどれかです。")
        self.log(f"  CRC32 = {crc}")
        if read_count:
            speed = (read_count * BANK_SIZE) / dump_elapsed / 1024
            self.log(f"  所要時間 = {dump_elapsed:.1f}秒 "
                     f"(新規読み出し{read_count}バンク, 平均{speed:.1f} KB/s)")
        else:
            self.log(f"  所要時間 = {dump_elapsed:.1f}秒（全バンクがキャッシュ済みでした）")

        if self.db_entries:
            for e in self.db_entries:
                c = e.get("crc")
                if isinstance(c, bytes) and binascii.hexlify(c).decode() == crc:
                    self.log(f"  データベース一致: 『{e['name']}』")
                    break

        # HiROMでチェックサムが合わないときは、DSP-1がバスを乗っ取っている疑いがある。
        # $00-$3F の $6000-$7FFF はDSP-1の応答になりROMが読めないが、$C0以降のミラーには
        # DSP-1が居ないので、そちらから読み直せば通る（スーパーマリオカートで実証済み）。
        if not ok and mapping == "hirom" and start_bank == 0 and not self.cancel_flag.is_set():
            self.log("チェックサムが合いません。DSP-1搭載カートの可能性があるため、"
                     "$C0 から読み直します。")
            self.log("  （DSP-1は $00-$3F の $6000-$7FFF に居座るためROMが隠れます）")
            if retry is not None:
                return retry(0xC0)

        final = out if ok else out + ".unverified"
        with open(final, "wb") as f:
            f.write(rom)
        self.log(f"保存: {final}")

        if ok:
            self.after(0, lambda: messagebox.showinfo(
                "ダンプ成功", f"チェックサム一致。\nCRC32 = {crc}\n{final}"))
        else:
            self.after(0, lambda: messagebox.showwarning(
                "チェックサム不一致", f"検証を通らなかったため .unverified で保存しました。\n{final}"))


    def _dump_fast(self, port, banks, mapping, out, start_bank=0, confirm=True):
        """1接続で全バンクを続けて読む（バンクごとにリセットしない）。

        ■ なぜ速いのか
        バンクごと読みは1バンクごとにNanoをリセットして開き直す。
        その待ちが支配的で、実測 9.5 KB/s しか出ない。
        連続読みは接続を張ったまま流し込むので 44 KB/s 出る（4MBで95秒）。

        ■ なぜ最初からこれにしなかったのか
        昔はストローブを取りこぼして化けたため、1バンクずつ読み直す方式に逃げていた。
        取りこぼしはピン変化割り込み(PCINT)で直っており、SA-1の連続読みが
        実際に通ることで裏が取れた。**バンクごと読みは、もう回避策としては要らない。**

        ■ 検証
        既定では2回読んで完全一致を確認する（それでも従来より速い）。
        一致しなければ化けているので、バンクごと読みに落とすよう促す。
        """
        from dump_sa1 import burst
        t0 = time.time()
        timing = TIMING_TIERS[0][:3]
        self.status_var.set("連続読み（1回目）")
        self.log(f"連続読み: バンク ${start_bank:02X} から {banks} バンクを一気に読みます")
        raw = burst(port, start_bank, banks, timing, True, self.log)
        if raw is None:
            self.log("読み出しに失敗しました。ポートとファームを確認してください。")
            return
        read_count = banks
        if confirm and not self.cancel_flag.is_set():
            self.status_var.set("連続読み（2回目・照合）")
            self.log("同じ範囲をもう一度読んで照合します")
            raw2 = burst(port, start_bank, banks, timing, True, self.log)
            if raw2 is None:
                self.log("2回目が読めませんでした。照合できないので中止します。")
                return
            diff = sum(1 for a, b in zip(raw, raw2) if a != b)
            read_count = banks * 2
            if diff:
                self.log(f"**2回の読み出しが {diff} バイト違います。** 保存しません。")
                self.log("  接触かタイミングの問題です。"
                         "「2回読んで一致するまで繰り返す」方式に切り替えてください。")
                self.status_var.set("不一致")
                return
            self.log("2回の読み出しが完全に一致しました")
        self._finish_dump(raw, mapping, out, start_bank,
                          time.time() - t0, read_count,
                          retry=lambda sb: self._dump_fast(port, banks, mapping, out,
                                                           start_bank=sb, confirm=confirm))

    # -- タイミング掃引 ---------------------------------------------

    def start_sweep(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        mapping = self.mapping_var.get()
        if mapping == "auto":
            messagebox.showerror("マッピング未確定",
                                 "「カートを判定」を実行するか、LoROM/HiROMを選んでください。")
            return
        self.run_worker(lambda: self._sweep(port, mapping))

    def _sweep(self, port, mapping, bank=0):
        """同じバンクを各段階で2回ずつ読み、相違と0xFF率を並べる。

        「読むたび内容が変わる」は電源・接触・マッパー・タイミング不足のどれでも起きる。
        この掃引はそれを切り分けるためにある。読み方は3通りに分かれる:

          ・段階を遅くすると相違が単調に減る → **タイミングマージン不足**。
            遅い段階で読めば済む。票を積むのは時間の無駄
          ・遅くしても相違が変わらず、0xFF率が読むたび動く → **接触**。
            挿し直しが最優先。それでも駄目なら多数決
          ・遅くすると**悪化する** → Super FX。相手がバスを持っているので最速のまま
            回数で殴る（段階は「高速」に固定する）

        0xFF率が全段階で動かないなら、その0xFFは本物のデータであって読み落としではない。
        """
        half = BANK_SIZE // 2
        self.log(f"タイミング掃引を開始します（bank {bank} を各段階で2回ずつ）。"
                 f"3分ほどかかります。")
        rows = []
        for idx, tier in enumerate(TIMING_TIERS):
            reads = []
            for _ in range(2):
                if self.cancel_flag.is_set():
                    return
                self.status_var.set(f"掃引 [{tier[3]}]")
                d = _read_bank_once(port, bank, tier, cancel_flag=self.cancel_flag,
                                    log=self.log)
                if d is None:
                    break
                reads.append(d[half:] if mapping == "lorom" else d)
            if len(reads) != 2:
                self.log(f"  [{tier[3]:6s}] 読み出しに失敗しました")
                continue
            diff = sum(1 for a, b in zip(*reads) if a != b)
            ff = [r.count(0xFF) / len(r) for r in reads]
            rows.append((idx, tier, diff, ff))
            self.log(f"  [{tier[3]:6s}] 相違 {diff:6d}/{len(reads[0])}  "
                     f"0xFF率 {ff[0]:.4f}/{ff[1]:.4f}")

        if not rows:
            self.log("掃引できませんでした。配線とポートを確認してください。")
            return

        # まず段階に対する傾向を見る。0xFF率の揺れは「相違が段階に反応しない」ときの
        # 決め手として使う。Super FX でも0xFF率は動く（GSUがバスを握った読みが0xFFになる）
        # ので、揺れだけで接触と決めつけると誤診する。
        allff = [v for _, _, _, ff in rows for v in ff]
        ff_swing = max(allff) - min(allff)
        diffs = [d for _, _, d, _ in rows]
        trend = _trend(diffs)

        clean = [r for r in rows if r[2] == 0]
        good = [r for r in rows if r[2] <= 8]
        if not any(diffs):
            self.log("→ どの段階でも相違0でした。このカートは素直に読めます。")
        elif trend == "down":
            self.log("→ 遅くすると相違が減っています。タイミングマージン不足です。"
                     "遅い段階で読めば済むので、票を積む必要はありません。")
            if ff_swing <= 0.01:
                self.log(f"  0xFF率は {min(allff):.4f}〜{max(allff):.4f} でほぼ動きません。"
                         "この0xFFは本物のデータで、読み落としではありません。")
        elif trend == "up":
            self.log("→ 遅くすると悪化しています。Super FX のようにカート側がバスを"
                     "持っている可能性があります。段階は「高速」のまま、"
                     "回数（サンプル数）で殴ってください。")
        elif ff_swing > 0.01:
            self.log(f"→ 段階を変えても相違が減らず、0xFF率が {ff_swing:.3f} 揺れています。"
                     "接触です。まずカートを挿し直すのが最短で、"
                     "それで駄目なら多数決マージに進んでください。")
        else:
            self.log("→ 段階を変えても改善せず、0xFF率も動きません。"
                     "電源やNano間の配線を疑ってください。")

        pick = (clean or good or rows)[0]
        self.tier_var.set(pick[1][3])
        self.log(f"タイミングを [{pick[1][3]}] に設定しました（相違 {pick[2]}）。")
        if pick[2] == 0:
            self.log("  この段階なら2回一致が取れるので、方式は「2回読んで一致」で足ります。")
        else:
            self.log("  相違が残るので、多数決マージで押し切るのが確実です。")

    # -- 多数決マージ方式 -------------------------------------------

    def _vote_tier_index(self, sample_idx, bump=0):
        """このサンプルをどの段階で読むかを決める。

        「自動巡回」は1本ごとに段階を変える。同じ条件で読み続けると同じ場所が同じように
        化けやすく、票が割れないまま間違いが多数派に居座るため。
        bump は追加取得のたびに1段ずつ遅くするための下駄。
        """
        label = self.tier_var.get()
        if label == "自動巡回":
            return (sample_idx + bump) % len(TIMING_TIERS)
        labels = [t[3] for t in TIMING_TIERS]
        base = labels.index(label) if label in labels else 0
        return min(base + bump, len(TIMING_TIERS) - 1)

    def _read_vote_sample(self, port, bank, cache, mapping, total_banks, bump, label,
                          save=True):
        """1本だけ読んでキャッシュに保存する。票にできなければ None。

        save=False はカートの照合用。まだ「同じカートか」が分かっていない読みを
        キャッシュに書くと、取り違えを検出して中断しても別カートのサンプルがファイルとして
        残り、次回それが票として読み込まれてしまう。
        """
        idx = len(glob.glob(os.path.join(cache, f"bank_{bank:03d}_s*.bin")))
        tier = TIMING_TIERS[self._vote_tier_index(idx, bump)]
        self.status_var.set(label)
        data = _read_bank_once(port, bank, tier, total_banks=total_banks,
                               cancel_flag=self.cancel_flag, log=self.log)
        if data is None:
            if not self.cancel_flag.is_set():
                self.log(f"    [{tier[3]}] 読み出し失敗")
            return None
        if all(b == data[0] for b in data):
            # 0xFF一色の読みは安定していて「2回一致」を平気で通す。票に入れると
            # 本物より多数派になりうるので、ここで捨てる。
            self.log(f"    [{tier[3]}] 全バイトが 0x{data[0]:02x}。カートが浮いています。破棄")
            return None
        if save:
            with open(os.path.join(cache, f"bank_{bank:03d}_s{idx:02d}.bin"), "wb") as f:
                f.write(data)
        return slice_for(data, mapping)

    def _dump_vote(self, port, banks, mapping, out, samples):
        """同じバンクを何度も読み、バイトごとの多数決で確定させる。

        2回一致方式が通らないカート用。生サンプルは <出力名>.votes/ に1本ずつ残るので、
        中断しても、あとからサンプル数を増やしても、不足分だけ読み足せる。

        サンプルは「バンクを一周しながら1本ずつ」集める。同じバンクを連続で読むと、
        その全部が「接触が悪い数分」に入りうるため（実測で品質が数分の間に12倍動いた）。
        """
        cache = out + ".votes"
        os.makedirs(cache, exist_ok=True)
        chunk = BANK_SIZE // 2 if mapping == "lorom" else BANK_SIZE
        self.log(f"多数決マージでダンプします（{mapping.upper()} / {banks}バンク / "
                 f"1バンクあたり{samples}本 / タイミング: {self.tier_var.get()}）")
        self.log(f"キャッシュ: {cache}")

        order = list(range(banks))
        pools = {}
        for b in order:
            got = []
            for f in sorted(glob.glob(os.path.join(cache, f"bank_{b:03d}_s*.bin"))):
                if os.path.getsize(f) == BANK_SIZE:
                    with open(f, "rb") as fh:
                        got.append(slice_for(fh.read(), mapping))
            pools[b] = got

        have = sum(len(v) for v in pools.values())
        if have:
            cached_title = next((t for t in (title_of(s) for s in pools[0]) if t), None)
            self.log(f"既存のサンプル {have} 本を再利用します"
                     f"（キャッシュのカート: {cached_title or 'ヘッダを読めず不明'}）")
            # キャッシュが信頼しているのは出力ファイル名だけで、カートの同一性は誰も
            # 見ていない。差し替えて同じ名前で走らせると別のゲームが黙って混ざる。
            probe = self._read_vote_sample(port, 0, cache, mapping, banks, 0,
                                           "カート照合中", save=False)
            if self.cancel_flag.is_set():
                return
            if probe is not None:
                now = title_of(probe)
                if cached_title and now and now != cached_title:
                    msg = (f"キャッシュのカートは「{cached_title}」ですが、"
                           f"今挿さっているのは「{now}」です。\n\n"
                           f"混ぜると壊れたROMができます。\n{cache} を削除するか、"
                           f"出力ファイル名を変えてください。")
                    self.log(msg.replace("\n\n", " ").replace("\n", " "))
                    self.after(0, lambda: messagebox.showerror("カートが違います", msg))
                    return
                pools[0].append(probe)
                if now:
                    self.log(f"  照合OK: {now}")

        dump_start = time.time()
        total_reads = banks * samples
        self.after(0, lambda: self.progress.config(maximum=total_reads, value=have))

        # --- 目標本数まで、バンクを一周しながら1本ずつ集める -------------------
        for rnd in range(samples):
            todo = [b for b in order if len(pools[b]) <= rnd]
            if not todo:
                continue
            self.log(f"── {rnd + 1}周目（{len(todo)}バンク）")
            for b in todo:
                if self.cancel_flag.is_set():
                    self.log("中止しました。読んだサンプルはキャッシュに残っています。")
                    return
                data = self._read_vote_sample(
                    port, b, cache, mapping, banks, 0,
                    f"bank {b}／{rnd + 1}周目")
                if data is not None:
                    pools[b].append(data)
                done = sum(len(v) for v in pools.values())
                self.after(0, lambda v=done: self.progress.config(value=v))

        empty = [b for b in order if not pools[b]]
        if empty:
            self.log(f"サンプルが1本も無いバンクがあります: "
                     f"{', '.join('$%02X' % b for b in empty)}")
            self.after(0, lambda: messagebox.showerror(
                "読み出し失敗", "1本も読めなかったバンクがあります。配線を確認してください。"))
            return

        results = {}
        pending = []
        for b in order:
            data, share, unresolved, info = merge_samples(_stack(pools[b]))
            results[b] = (data, unresolved)
            self.log(f"bank ${b:02X}: {len(pools[b])}本 → 未決着 {unresolved.size} バイト"
                     f" / 窓の除外 {info['excluded_windows']}/{info['total_windows']}"
                     + (f" / ビット再構成 {info['bit_rebuilt']}"
                        if "bit_rebuilt" in info else ""))
            if unresolved.size:
                pending.append(b)

        # --- 決着していないバンクにだけ、段階を上げながら票を足す ---------------
        # 票を増やしても駄目なら微秒を払う。原因が接触なら遅くしても変わらないが、
        # タイミング不足なら劇的に減る。どちらかは事前に分からないので両方に賭ける。
        fails = {b: 0 for b in order}
        bump = 0
        max_samples = max(samples * 3, 15)
        while pending and not self.cancel_flag.is_set():
            pending = [b for b in pending if len(pools[b]) < max_samples]
            if not pending:
                break
            bump += 1
            if self.tier_var.get() == "自動巡回":
                note = "／ 段階を巡回しながら読みます"
            else:
                note = (f"／ タイミングを "
                        f"[{TIMING_TIERS[self._vote_tier_index(0, bump)][3]}] に上げます")
            self.log(f"── 追加取得: 未決着の残る {len(pending)} バンク {note}")
            still = []
            for b in pending:
                if self.cancel_flag.is_set():
                    return
                data = self._read_vote_sample(port, b, cache, mapping, banks, bump,
                                              f"bank {b} 追加取得")
                if data is None:
                    fails[b] += 1
                    if fails[b] >= 3:
                        self.log(f"bank ${b:02X}: 3回続けて読めませんでした。打ち切ります"
                                 f"（未決着 {results[b][1].size} バイトが残ります）")
                    else:
                        still.append(b)
                    continue
                fails[b] = 0
                pools[b].append(data)
                merged, share, unresolved, info = merge_samples(_stack(pools[b]))
                results[b] = (merged, unresolved)
                self.log(f"bank ${b:02X}: {len(pools[b])}本 → 未決着 "
                         f"{unresolved.size} バイト")
                if unresolved.size:
                    still.append(b)
            pending = still

        if self.cancel_flag.is_set():
            self.log("中止しました。読んだサンプルはキャッシュに残っています。")
            return

        rom = b"".join(results[b][0] for b in order)
        total_unresolved = sum(int(results[b][1].size) for b in order)
        elapsed = time.time() - dump_start

        ok, computed, expected, size = verify_checksum(rom, mapping)
        if ok and size != len(rom):
            self.log(f"実体は {size} bytes ({size // 1024}KB) でした。"
                     f"末尾 {len(rom) - size} bytes はミラーなので切り落とします。")
            rom = rom[:size]
        crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")

        self.log(f"合計 {len(rom)} bytes / 未決着 {total_unresolved} バイト")
        self.log(f"  チェックサム 計算値="
                 f"{('0x%04x' % computed) if computed is not None else 'NA'} 期待値="
                 f"{('0x%04x' % expected) if expected is not None else 'NA'} → "
                 f"{'一致' if ok else '不一致'}")
        self.log(f"  CRC32 = {crc}")
        self.log(f"  所要時間 = {elapsed:.0f}秒")

        matched = None
        for e in self.db_entries or []:
            c = e.get("crc")
            if isinstance(c, bytes) and binascii.hexlify(c).decode() == crc:
                matched = e["name"]
                self.log(f"  データベース一致: 『{matched}』")
                break

        final = out if ok else out + ".unverified"
        with open(final, "wb") as f:
            f.write(rom)
        self.log(f"保存: {final}")
        if total_unresolved:
            self.log(f"  未決着が {total_unresolved} バイト残っています。"
                     f"サンプル数を増やして再実行すると、不足分だけ読み足します。")

        if ok:
            extra = f"\nデータベース一致: {matched}" if matched else ""
            self.after(0, lambda: messagebox.showinfo(
                "ダンプ成功", f"チェックサム一致。\nCRC32 = {crc}{extra}\n{final}"))
        else:
            self.after(0, lambda: messagebox.showwarning(
                "チェックサム不一致",
                f"検証を通らなかったため .unverified で保存しました。\n"
                f"未決着 {total_unresolved} バイト\n{final}"))


def _trend(diffs):
    """相違の数が段階に対してどちらへ動いているかを返す。

    「最後 > 最初」だけで増加と決めると、段階に反応しない（＝接触が原因の）カートが
    ばらつきの偶然で Super FX と誤診される。**倍以上の変化があり、かつ途中で大きく
    逆行していない**ことを両方求める。
    """
    if len(diffs) < 2:
        return "flat"
    if diffs[-1] * 2 <= diffs[0] and all(b <= a * 1.2 + 4 for a, b in zip(diffs, diffs[1:])):
        return "down"
    if diffs[0] * 2 <= diffs[-1] and all(b >= a * 0.8 - 4 for a, b in zip(diffs, diffs[1:])):
        return "up"
    return "flat"


def _stack(samples):
    import numpy as np
    return np.stack([np.frombuffer(s, dtype=np.uint8) for s in samples])


if __name__ == "__main__":
    app = DumperApp()
    app.mainloop()
