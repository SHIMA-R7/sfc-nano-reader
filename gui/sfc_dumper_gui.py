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
import tkinter as tk
from collections import Counter
from tkinter import filedialog, messagebox, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

import rdb

BANK_SIZE = 65536
DEFAULT_BAUD = 250000

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
    """生ダンプからROM本体を取り出す。LoROMはバンク後半がミラーなので捨てる。"""
    if mapping == "hirom":
        return raw
    out = bytearray()
    for i in range(0, len(raw), BANK_SIZE):
        out += raw[i:i + BANK_SIZE // 2]
    return bytes(out)


def read_header(rom, off):
    """指定オフセットのヘッダを読む。妥当そうなら dict、駄目なら None。"""
    if len(rom) < off + 32:
        return None
    title = rom[off:off + 21]
    map_mode = rom[off + 0x15]
    rom_size_byte = rom[off + 0x17]
    complement = rom[off + 28] | (rom[off + 29] << 8)
    checksum = rom[off + 30] | (rom[off + 31] << 8)
    if ((checksum + complement) & 0xFFFF) != 0xFFFF:
        return None
    if checksum == 0:
        return None
    printable = sum(1 for b in title if 0x20 <= b < 0x7F)
    if printable < 4:
        return None
    return {
        "title": title.decode("ascii", errors="replace").strip(),
        "map_mode": map_mode,
        "size_kb": 1 << rom_size_byte if rom_size_byte < 20 else None,
        "checksum": checksum,
        "complement": complement,
    }


def detect_mapping(raw_one_bank):
    """1バンク分の生データから (mapping, header) を推定する。"""
    lo = read_header(raw_one_bank, 0x7FC0)
    hi = read_header(raw_one_bank, 0xFFC0)
    if hi and not lo:
        return "hirom", hi
    if lo and not hi:
        return "lorom", lo
    if lo and hi:
        # 両方それらしい場合は map_mode バイトで判断（0x21/0x30台がHiROM系）
        return ("hirom", hi) if (hi["map_mode"] & 0x01) else ("lorom", lo)
    return None, None


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
        self.geometry("900x820")
        self.minsize(780, 700)

        self.log_queue = queue.Queue()
        self.worker = None
        self.cancel_flag = threading.Event()
        self.samples = []
        self.cli_path = find_arduino_cli()
        self.db_entries = None      # 遅延読み込み
        self.db_hits = []
        self.db_size_bytes = None   # DBで選んだタイトルの実サイズ

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
        ttk.Checkbutton(dump, text="チェックサムが一致するまで繰り返してマージ（OR / 多数決）",
                        variable=self.repeat_var).grid(row=3, column=0, columnspan=4, sticky="w", padx=6, pady=4)

        btns = ttk.Frame(dump)
        btns.grid(row=4, column=0, columnspan=5, sticky="w", padx=6, pady=6)
        self.identify_btn = ttk.Button(btns, text="カートを判定 (1バンクだけ読む)", command=self.start_identify)
        self.identify_btn.pack(side="left")
        self.dump_btn = ttk.Button(btns, text="ダンプ開始", command=self.start_dump)
        self.dump_btn.pack(side="left", padx=6)
        self.cancel_btn = ttk.Button(btns, text="中止", command=self.cancel, state="disabled")
        self.cancel_btn.pack(side="left")

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

    def _receive(self, port, total_bytes, label=""):
        """1回分の生ダンプを受信。中止/失敗時は None。"""
        if serial is None:
            self.log("pyserial がありません。")
            return None
        try:
            ser = serial.Serial(port, DEFAULT_BAUD, timeout=25)
        except Exception as e:
            self.log(f"ポートを開けません: {e}")
            return None

        try:
            time.sleep(2)             # Nano自動リセット後の起動待ち
            ser.reset_input_buffer()  # 前回の残留バイトを捨てる

            buf = bytearray()
            start = time.time()
            self.after(0, lambda: self.progress.config(maximum=total_bytes, value=0))
            while len(buf) < total_bytes:
                if self.cancel_flag.is_set():
                    self.log("中止しました。")
                    return None
                chunk = ser.read(min(4096, total_bytes - len(buf)))
                if not chunk:
                    self.log(f"タイムアウト: {len(buf)}/{total_bytes} バイト")
                    return None
                buf += chunk
                got = len(buf)
                self.after(0, lambda v=got: self.progress.config(value=v))
                pct = got * 100 // total_bytes
                self.status_var.set(f"{label}受信中 {pct}%")
            self.log(f"受信完了 {len(buf)} bytes ({time.time() - start:.1f}s)")
            return bytes(buf)
        finally:
            ser.close()

    def start_identify(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        self.run_worker(lambda: self._identify(port))

    def _identify(self, port):
        self.log("カート判定のため1バンク分を読み取ります。")
        self.log("※ Nano-2 のスケッチの NUM_BANKS が 1 になっている必要があります。")
        raw = self._receive(port, BANK_SIZE, "判定用 ")
        if raw is None:
            return
        mapping, hdr = detect_mapping(raw)
        if not mapping:
            self.log("ヘッダを認識できませんでした。カートの接触や配線を確認してください。")
            return
        self.log(f"判定結果: {mapping.upper()}  タイトル『{hdr['title']}』")
        self.log(f"  ROMサイズ: {hdr['size_kb']} KB / チェックサム: {hex(hdr['checksum'])}")

        self.mapping_var.set(mapping)

        if self.db_size_bytes:
            # DBで明示的に選んだサイズがあるなら、そちらを優先する。
            # ヘッダの自己申告は2の累乗に切り上げられていることがあるため。
            self.log("  データベースで選択済みのサイズを優先します。")
            self.recompute_banks()
        elif hdr["size_kb"]:
            banks = hdr["size_kb"] * 1024 // (BANK_SIZE if mapping == "hirom" else BANK_SIZE // 2)
            self.banks_var.set(str(banks))
            self.log(f"  → バンク数を {banks} に設定しました。"
                     f"Nano-2 の NUM_BANKS も同じ値にして書き込み直してください。")

        if not self.db_size_bytes:
            safe = "".join(c for c in hdr["title"] if c.isalnum() or c in " _-").strip().replace(" ", "")
            if safe:
                self.out_var.set(os.path.join(PROJECT_ROOT, safe + ".sfc"))
            # ヘッダのタイトルでDBを自動検索しておく
            self.query_var.set(hdr["title"])
            self.after(0, self.search_db)

    def start_dump(self):
        port = self.selected_port()
        if not port:
            messagebox.showerror("ポート未選択", "シリアルポートを選んでください。")
            return
        try:
            banks = int(self.banks_var.get())
            if banks < 1:
                raise ValueError
        except ValueError:
            messagebox.showerror("入力エラー", "バンク数は1以上の整数で指定してください。")
            return
        out = self.out_var.get()
        if not out:
            messagebox.showerror("出力先未設定", "出力ファイルを指定してください。")
            return
        self.samples = []
        self.run_worker(lambda: self._dump(port, banks, out))

    def _dump(self, port, banks, out):
        total = banks * BANK_SIZE
        mapping_choice = self.mapping_var.get()
        repeat = self.repeat_var.get()
        round_no = 0

        while True:
            if self.cancel_flag.is_set():
                break
            round_no += 1
            self.log(f"--- ダンプ {round_no} 回目 ---")
            raw = self._receive(port, total, f"{round_no}回目 ")
            if raw is None:
                if self.cancel_flag.is_set():
                    break
                self.log("失敗したので再試行します。")
                continue

            self.samples.append(raw)

            mapping = mapping_choice
            if mapping == "auto":
                mapping, hdr = detect_mapping(raw[:BANK_SIZE])
                if not mapping:
                    self.log("マッピングを自動判定できません。LoROM/HiROMを手動指定してください。")
                    break
                self.log(f"自動判定: {mapping.upper()}")

            roms = [extract_rom(r, mapping) for r in self.samples]
            self.status_var.set("マージ中...")

            # ビット落ち方向の誤りが主なのでORを先に試し、駄目なら多数決も見る。
            candidates = [("OR", or_merge(roms))]
            disputed = 0
            if len(roms) > 1:
                maj, disputed = majority_merge(roms)
                candidates.append(("多数決", maj))

            hit = None
            for label, cand in candidates:
                ok, computed, expected = verify_checksum(cand, mapping)
                self.log(f"[{label}] サンプル{len(roms)}個 / 不一致バイト{disputed} / "
                         f"計算値={hex(computed) if computed is not None else 'NA'} "
                         f"期待値={hex(expected) if expected is not None else 'NA'} → "
                         f"{'一致' if ok else '不一致'}")
                if ok and hit is None:
                    hit = (label, cand)

            if hit:
                label, merged = hit
                with open(out, "wb") as f:
                    f.write(merged)
                self.log(f"完了: {label}マージで一致。{out} ({len(merged)} bytes) を保存しました。")
                self.after(0, lambda: messagebox.showinfo(
                    "ダンプ成功", f"チェックサム一致（{label}マージ）。\n{out}"))
                return
            merged = candidates[0][1]

            if not repeat:
                path = out + ".unverified"
                with open(path, "wb") as f:
                    f.write(merged)
                self.log(f"チェックサム不一致のまま保存しました: {path}")
                return

            self.log("チェックサムが合わないので、もう1回読み取ります。")


if __name__ == "__main__":
    app = DumperApp()
    app.mainloop()
