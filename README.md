# SFC (Super Famicom) Cartridge Reader — Nano x3

Arduino Nano 3台だけでスーパーファミコンのカートリッジROMを読み出すプロジェクト。
ESP32-S3やレベル変換IC、専用シフトレジスタを一切使わず、5VネイティブなNanoを
アドレス生成・データ読み取り・バンク切り替えの3役に分担させることで、
追加部品ゼロで構成しています。

## 構成

| ボード | 役割 |
|---|---|
| Nano-1 ([nano1_addr_low](nano1_addr_low/)) | カートアドレス A0-A15 の生成。STROBEパルスで16bitカウンタを+1 |
| Nano-2 ([nano2_master](nano2_master/)) | /RD・/ROMSEL制御、データバスD0-D7の読み取り、USBシリアルでPCへストリーム、SH1106 OLEDへ進捗表示 |
| Nano-3 ([nano3_bank](nano3_bank/)) | カートアドレス A16-A23（バンク）の生成。Nano-2からDATA/CLK/LATCHで受信 |

PC側は [host/receive.py](host/receive.py) でNano-2からのUSBシリアルストリームを受信してファイル保存する。

## 特徴・ハマりどころ

- SFCのカードエッジコネクタは62pin(表31+裏31)、ピッチは通称2.54mmではなく実測2.50mm。列間隔・キー溝も非標準
- LoROMはバンク内で上位/下位32KBが同一内容にミラーされる（A15がROM選択に関与しないため）。これは正常な電気的挙動
- Nano間の3線シフト転送(DATA/CLK/LATCH)は非同期クロックのため、パルス幅に十分なマージンが必要（実測で5us→300usに拡大して安定化）
- 複数回ダンプしてバイト単位多数決でマージすることで、電気的ノイズによる読み取りミスを吸収できる（[host/auto_dump_merge2.py](host/auto_dump_merge2.py)）

## 使い方

1. 62pinカードエッジコネクタ経由でカートと3台のNanoを配線（アドレス/データ/制御/GND/+5V）
2. `arduino-cli` で各ボードに対応スケッチを書き込み（`arduino:avr:nano:cpu=atmega328old` を推奨。クローン品はOld Bootloader指定が必要な場合あり）
3. `nano2_master.ino` の `NUM_BANKS` をカート容量に合わせて調整
4. `pip install pyserial`
5. `python host/receive.py <COMポート> <NUM_BANKS*65536> dump.bin`

信頼性を上げたい場合は `host/auto_dump_merge2.py` で複数回ダンプしてバイト単位多数決マージができます。

## 注意

- Super FX / SA-1 / DSP-1 などのコプロセッサ搭載カートはこの設計では読めません（ROMがコプロチップの内部バスにぶら下がっており、カート側コネクタから直接見えないため）
- 読み出したROMデータそのもの（`.sfc`/`.bin`）は著作物なので、このリポジトリには含めていません
