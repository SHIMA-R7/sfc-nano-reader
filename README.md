# SFC (Super Famicom) Cartridge Reader — Nano x3

Arduino Nano 3台だけでスーパーファミコンのカートリッジROMを読み出すプロジェクト。

ESP32-S3やレベル変換IC、シフトレジスタ(74HC595)を一切使わず、**5VネイティブなNanoを
「アドレス生成」「データ読み取り」「バンク生成」の3役に分担させる**ことで、
手持ちのNano以外の追加部品ゼロで構成しています。

![GUI](docs/gui.png)

## なぜNano×3なのか

SFCカートを読むには最低でも **アドレス24本 + データ8本 + 制御2本 ≒ 34本** のI/Oが要ります。

| 構成 | 問題点 |
|---|---|
| ESP32-S3 単体 | 5Vトレラントでないため、カートの5V出力を直結すると壊れる。レベル変換IC必須 |
| Arduino Uno 単体 | 5Vネイティブで電圧問題はないが、使えるI/Oが18本しかなく全く足りない |
| Arduino Mega 2560 | I/O 54本で直結でき最も素直（Sanni's Cart Readerがこの構成）。ただし買い足しが必要 |
| **Nano × 3** ← 本プロジェクト | **手持ちのみで完結。全ボードが5Vネイティブなので電圧問題が原理的に発生しない** |

Raspberry Pi Zero 2 を併用する案も検討しましたが、3.3V系のためデータバス8本＋ACKに
分圧抵抗が18本ほど必要になり、「手持ち部品だけで完結する」という利点が失われるため見送りました。

## 構成

```
                    ┌──────────────┐
   A0-A15 ─────────>│              │
                    │  SFCカート   │
   A16-A23 ────────>│  (62pin)     │
                    │              │
   /RD,/ROMSEL ────>│              │
                    └──────┬───────┘
                           │ D0-D7
                           v
  [Nano-1]  <--STROBE--  [Nano-2]  --STROBE-->  [Nano-3]
  A0-A15生成             マスタ/データ読取      A16-A23生成
                         + OLED + USB
                              │
                              v  USBシリアル (250000 bps)
                             PC
```

| ボード | 役割 | スケッチ |
|---|---|---|
| Nano-1 | カートアドレス **A0-A15** を生成。STROBEパルスで16bitカウンタを+1、RESETで0クリア | [nano1_addr_low/](nano1_addr_low/) |
| Nano-2 | `/RD`・`/ROMSEL`制御、**D0-D7**読み取り、USBシリアルでPCへストリーム、SH1106 OLEDへ進捗表示 | [nano2_master/](nano2_master/) |
| Nano-3 | カートアドレス **A16-A23**（バンク）を生成。Nano-1と同じくSTROBE方式 | [nano3_bank/](nano3_bank/) |

`/WR` と `/RESET` はカート側で +5V に直結（読み出し専用のため）。
CICロックアウトチップは**一切関係ありません**。ROMを直接バス読みするだけなので認証は絡みません。

## ソフトウェア

### GUIアプリ（推奨）

```bash
python gui/sfc_dumper_gui.py
```

ブラウザ不要、Python標準のTkinterのみ（追加依存は `pyserial` だけ）。

- **COMポート選択**と自動検出
- **ファームウェア書き込み** — Nano-1/2/3を選んでボタン1つ（`arduino-cli`のパスも自動発見）
- **ROMサイズの指定を3通りから選べる**
  1. **自動判定** — 1バンクだけ読んでヘッダからLoROM/HiROM・サイズ・タイトルを認識
  2. **手動** — バンク数とマッピングを直接入力
  3. **データベース検索** — タイトル名で検索して正確なバイト数を引く（後述）
- **チェックサムが一致するまで自動でダンプを繰り返し、バイト単位の多数決でマージ**

### CLI

```bash
# 単発ダンプ
python host/receive.py COM12 2097152 dump.bin

# チェックサムが合うまで繰り返して多数決マージ
python host/dump_until_valid.py --port COM12 --banks 32 --mapping hirom --out MyGame.sfc
```

### データベース検索について

RetroArchに同梱されている `database/rdb/Nintendo - Super Nintendo Entertainment System.rdb`
（No-Intro準拠、7,621件）をそのまま読んでいます。**ネットワークアクセスは不要**です。

このファイルはlibretro独自のバイナリ形式（MessagePack）なので、外部ライブラリを増やさずに
済むよう最小限のデコーダを [gui/rdb.py](gui/rdb.py) に実装しています。

DBを使う利点は、**ヘッダの自己申告が2の累乗に切り上げられている場合があるため**です。
例えば `Super Nazo Puyo Tsuu (Japan)` は実際には 1,572,864 bytes (1.5MB) ですが、
ヘッダのROMサイズバイトは2の累乗でしか表現できません。

## 使い方

1. 62pinカードエッジコネクタ経由でカートと3台のNanoを配線
2. GUIから各ボードにスケッチを書き込み（`arduino:avr:nano:cpu=atmega328old` を使用。
   クローン品はOld Bootloader指定でないと `programmer is not responding` になることが多い）
3. 「カートを判定」でROMサイズを特定し、`nano2_master.ino` の `NUM_BANKS` をその値に変更して再書き込み
4. 「ダンプ開始」

## ハマりどころ（実際に踏んだもの）

### コネクタ

- SFCのカードエッジは **62pin = 表31 + 裏31**。表裏で124本ではない
- ピッチは通称2.54mmではなく **実測2.50mm**。さらに列間隔とキー溝の位置も非標準なので、
  2.54mmグリッドのユニバーサル基板とは完全には整合しない
- 62本のうち相当数はGND/VCCの重複。実際に配線が必要なユニーク信号は
  アドレス24 + データ8 + 制御2 + 電源/GND数本で足りる

### メモリマップ

- **LoROMはバンク内で上位/下位32KBが同一内容にミラーされる**（A15がROM選択に関与しないため）。
  これは故障ではなく正常な電気的挙動。ダンプ後に前半32KBだけ抽出する
- HiROMは1バンク64KBをフルに使う（ミラーなし）

### タイミング

- Nano間の3線シフト転送（DATA/CLK/LATCH）は、2台が**別々の水晶発振子で非同期に動く**ため
  クロック同期でビットを取りこぼす。パルス幅を5us→300usに広げても完全には安定しなかった
- 最終的に **Nano-1と同じ「STROBEでカウントアップ」方式に統一**して解決。
  クロック同期という概念自体が消えるため原理的に安定する（誤り率が数十万バイト→数千バイトに激減）
- ROMのアクセスタイム自体は120〜200ns程度だが、長い配線の寄生容量を考慮して
  `/RD`アサート後の待ち時間は100usと大きめに取っている

### 対応できないカート

Super FX / SA-1 / DSP-1 / S-DD1 / SPC7110 / CX4 などの**コプロセッサ搭載カートは読めません**。
ROMがコプロチップの内部バスにぶら下がっており、カートのエッジコネクタから直接見えないためです。
（スターフォックス、ヨッシーアイランド、スーパーマリオRPG、スーパーマリオカート等）

## 注意

読み出したROMデータそのもの（`.sfc` / `.bin`）は著作物なので、このリポジトリには含めていません。
自分が正規に所有するカートリッジのバックアップ用途を想定しています。

## 謝辞 / Acknowledgements

このプロジェクトは、先人が公開してくださった資料・ソフトウェアなしには成立しませんでした。
以下すべてに深く感謝します。

### ハードウェア資料

- **[SNESdev Wiki — Cartridge connector](https://snes.nesdev.org/wiki/Cartridge_connector)**
  62pinカードエッジコネクタの完全なピンアサインを参照しました。本プロジェクトの配線表は
  ここの情報が基になっています。
- **[NESdev Wiki — Cartridge connector](https://www.nesdev.org/wiki/Cartridge_connector)**
  コネクタ全般の背景知識について。
- **[NESdev Forums — SNES Edge Connector](https://forums.nesdev.org/viewtopic.php?t=11389)**
  「SNESコネクタは2.54mmではなく2.50mmピッチであり、列間隔とキー溝も非標準」という、
  自作時に最も重要な指摘をここで得ました。
- **[shmups.system11.org — SNES 62-Pin Connector Replacement Dilemma](https://shmups.system11.org/viewtopic.php?t=53375)**
  互換コネクタの実情について。
- **[Sanni's Open Source Cart Reader](https://github.com/sanni/cartreader)**
  カートリッジダンパーの事実上の標準実装。ピンアサインとメモリマップ処理の
  正解を確認する基準として参照しました。Arduino Megaを使う理由（5Vネイティブ＋I/O数）も
  ここから学んでいます。

### ソフトウェア / ライブラリ

- **[U8g2 by olikraus](https://github.com/olikraus/u8g2)** (BSD-2-Clause)
  SH1106 OLEDの駆動に使用。ハードウェアI2Cピンが埋まっていたため、
  ソフトウェアI2C対応が決定的に助かりました。
- **[libretro-database / RetroArch](https://github.com/libretro/libretro-database)**
  タイトル検索とROMサイズ特定に使用している `.rdb` データベースの提供元。
- **[No-Intro](https://no-intro.org/)**
  上記データベースの元となっているROMセット情報の編纂プロジェクト。
- **[Snes9x](https://github.com/snes9xgit/snes9x)** / **[RetroArch](https://www.retroarch.com/)**
  ダンプ結果の検証に使用。特にSnes9xコアが出力する `Checksum OK` / `Invalid Checksum` の
  判定は、「ヘッダだけ正しくて本体が壊れている」状態を発見する決め手になりました。
- **[pyserial](https://github.com/pyserial/pyserial)**
  PC側のシリアル通信全般。
- **[arduino-cli](https://github.com/arduino/arduino-cli)**
  GUIからのコンパイル・書き込み自動化。

### その他

- SNESのメモリマップ、内部ヘッダ構造（チェックサム/補数の配置、ROMサイズバイトのlog2表現）
  に関する知見は、上記NESdev系Wikiのコミュニティが長年かけて解析・公開してくれたものです。

### 製作者より

- こんにちは、ここまでのReadmeはすべてClaudeCodeが書いてくれたものです。お作法などよろしくないところも多そうですが、どうかご勘弁してください。さて、このダンパーは自分の力試しの側面がとても大きいです。というのも、「追加で一つもパーツを購入しない」という縛りプレイで制作したからです。62ピンのコネクターなどというぞっとしないパーツを使ってるにもかかわらず、PCBを使ってないのもそういうわけです。ユニバーサル基盤は2.54mmしかなかったので、カートリッジコネクターの一部を超音波カッターでぶった切る、無理やり結束バンドで止めるなどの無茶をやっています。結線はNano↔カートリッジはすべてUEW、それ以外はAWG22のビニール導線です。このような無茶苦茶な作り方をしても、一応動くようにはなるんだから、SFCってのはやっぱり世界一頑丈なハードなんじゃないかな、と思います。パスコンはあったほうがいいですよ！あと、最初は電源が足りないかなと思いましたが、思いのほかデータ通信用につないだケーブル一本で作動します。最後に、実用性についてですが、まぁ手間に合いませんな。ありあわせのパーツで動かすにはもってこいですが、安く仕上げたいんじゃなかったら絶対ほかのプロジェクトや既製品を参考にした方がいいです。1カートリッジあたり二桁会は吸い出さないとまともなデータが手に入らないのでね。

## ライセンス

MIT License（[LICENSE](LICENSE) を参照）
