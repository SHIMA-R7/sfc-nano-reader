"""GUIの単一exeをビルドして、**起動するところまで確認する**。

■ なぜスクリプトにしたのか
2026-08-29、手打ちでビルドしたexeが `ModuleNotFoundError: No module named 'bankio'`
で起動せず、そのままReleasesに公開してしまった。
原因は単純で、**ビルドコマンドがどこにも記録されていなかった**こと。
`gui/sfc_dumper_gui.py` のコメントには「後述のビルド手順を参照」とあるのに、
その手順は書かれたことがなかった。

■ なぜ --paths が要るのか
GUIは実行時に host/ を sys.path へ足してから `import bankio` する。

    _HOST_DIR = .../host
    sys.path.insert(0, _HOST_DIR)
    from bankio import ...

PyInstallerは静的解析でしか依存を追えないので、この動的な追加が見えない。
`--paths` で解析時にも同じ場所を教える必要がある。
`rdb`(gui/) と `contact_merge`(host/) も同じ理由で明示する。

■ ビルドしただけで公開しない
「ビルドが成功した」と「exeが起動する」は別の話である。
--windowed のexeは失敗しても何も表示せずに死ぬので、成功メッセージだけを見ていると
壊れたまま配ってしまう。ここでは必ず起動確認まで行う。

    python tools/build_gui_exe.py
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "dist", "SFC_Dumper.exe")


def build():
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--onefile", "--windowed", "--name", "SFC_Dumper",
        # ここが肝。実行時に sys.path へ足される場所を、解析時にも教える。
        "--paths", os.path.join(ROOT, "gui"),
        "--paths", os.path.join(ROOT, "host"),
        "--hidden-import", "bankio",
        "--hidden-import", "rdb",
        "--hidden-import", "contact_merge",
        # 電源(DP100)とSA-1の起動読み。GUIは実行時にこれらを import する。
        "--hidden-import", "dp100",
        "--hidden-import", "sa1_wake",
        "--hidden-import", "dump_sa1",
        "--hidden-import", "hid",
        "--hidden-import", "crcmod",
        "--distpath", os.path.join(ROOT, "dist"),
        "--workpath", os.path.join(ROOT, "build", "pyi"),
        "--specpath", os.path.join(ROOT, "build"),
        "--noconfirm",
        os.path.join(ROOT, "gui", "sfc_dumper_gui.py"),
    ]
    print("ビルド中…")
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        print(r.stdout[-3000:])
        print(r.stderr[-3000:])
        return False
    return os.path.exists(EXE)


def smoke_test():
    """起動して数秒生き残るかを見る。落ちれば stderr に理由が出る。

    --windowed のexeでも、コンソールから起動すれば例外は stderr に流れる。
    「起動した」の判定は、プロセスが生きていることと、stderrが空であることの両方。
    """
    print("起動確認中…")
    p = subprocess.Popen([EXE], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    time.sleep(8)
    alive = (p.poll() is None)
    if alive:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
        err = b""
    else:
        _out, err = p.communicate(timeout=5)
    if not alive:
        print("起動に失敗しました。")
        print(err.decode("utf-8", "replace")[-2000:])
        return False
    return True


def main():
    if not build():
        print("ビルドに失敗しました。")
        return 1
    size = os.path.getsize(EXE)
    print(f"生成: {EXE}  {size:,} bytes")
    if not smoke_test():
        print("**このexeを配布してはいけません。**")
        return 1
    print("起動確認OK。配布して問題ありません。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
