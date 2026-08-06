"""
SFC Cartridge Dumper GUI

Nano x3 構成のカートリッジリーダーを、ファーム書き込みからダンプ・マージまで
GUIだけで操作するためのアプリ。ブラウザ不要、Python標準のTkinterのみで動く。

    python sfc_dumper_gui.py
"""

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
    off = 0xFFC0 if mapping == "hirom" else 0x7FC0
    hdr = read_header(rom, off)
    if not hdr:
        return False, None, None
    computed = sum(rom) & 0xFFFF
    return computed == hdr["checksum"], computed, hdr["checksum"]


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

        self._build_ui()
        self.refresh_ports()
        self.after(100, self._drain_log)
        threading.Thread(target=self._load_db, daemon=True).start()

        if serial is None:
            self.log("pyserial が見つかりません。`pip install pyserial` を実行してください。")

    # -- UI 構築 -------------------------------------------------

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

        self.repeat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(dump, text="各バンクを2回読んで一致するまで繰り返す（推奨）",
                        variable=self.repeat_var).grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=4)

        btns = ttk.Frame(dump)
        btns.grid(row=4, column=0, columnspan=5, sticky="w", padx=6, pady=6)
        self.identify_btn = ttk.Button(btns, text="カートを判定 (1バンクだけ読む)", command=self.start_identify)
        self.identify_btn.pack(side="left")
        self.dump_btn = ttk.Button(btns, text="ダンプ開始", command=self.start_dump)
        self.dump_btn.pack(side="left", padx=6)
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
        state = "disabled" if busy else "normal"
        self.identify_btn.config(state=state)
        self.dump_btn.config(state=state)
        self.sram_btn.config(state=state)
        self.cancel_btn.config(state="normal" if busy else "disabled")

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

        dump_elapsed = time.time() - dump_start

        rom = extract_rom(b"".join(collected), mapping)
        ok, computed, expected = verify_checksum(rom, mapping)
        crc = format(zlib.crc32(rom) & 0xFFFFFFFF, "08x")

        self.log(f"合計 {len(rom)} bytes")
        self.log(f"  チェックサム 計算値=0x{computed:04x} 期待値="
                 f"{('0x%04x' % expected) if expected is not None else 'NA'} → "
                 f"{'一致' if ok else '不一致'}")
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
            return self._dump(port, banks, mapping, out, start_bank=0xC0)

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


if __name__ == "__main__":
    app = DumperApp()
    app.mainloop()
