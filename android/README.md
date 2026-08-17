# SFC-Dumper Android アプリ

PCを使わず、Android端末とNano-2(マスタ)をUSB(OTG)で直結してカートをダンプするアプリ。
`host/bankio.py` と `gui/sfc_dumper_gui.py` のロジックをKotlinへ移植したもの。

**ファームウェア(nano1/nano2/nano3)は一切変更していない。** PC版と同じスケッチのまま動く。

実体のAndroid Studioプロジェクトは `C:\Users\<user>\AndroidStudioProjects\SFCDumper`。
このフォルダはソースの参照・バックアップ用。

## 構成

| ファイル | 内容 |
|---|---|
| `CartReader.kt` | Nano-2とのUSBシリアル通信。タイミング段階制御、2回読み一致検証、`AdaptiveTiming` |
| `RomAnalyzer.kt` | LoROM/HiROM判定、ヘッダ解析、ROM本体の抽出、チェックサム検証 |
| `DumpJob.kt` | カート判定 → 全バンク読み出し → ROM抽出 → 検証 の流れ |
| `MainActivity.kt` | 画面、USB接続・権限処理、SAFでの保存先選択 |

## ビルド

Android Studio で `Empty Views Activity` / Kotlin / minSdk 26 /
package `com.shvc.sfcdumper` のプロジェクトを作り、上記ファイルを配置する。
依存関係は `app/build.gradle.kts` と `settings.gradle.kts` を参照
(`usb-serial-for-android` を JitPack 経由で取得)。

## Android移植で判明した注意点

実機で動かすまでに3つの落とし穴があった。同じことをやる人向けに記録しておく。

### 1. 受信は必ずチャンク単位で扱う

`SerialInputOutputManager` の `onNewData()` で受け取ったデータを、1バイトずつ
`LinkedBlockingQueue<Byte>` に詰める実装にすると **1,000,000bps では取りこぼす**。
64KBのバンク1本で65536回のボクシングが発生し、USB受信スレッドが処理に追いつかなくなる。
実際に「59642/65536 で止まる」という症状が出た。
届いた配列をそのままキューに積む方式(`LinkedBlockingQueue<ByteArray>`)にすれば解決する。
併せて `readBufferSize` も既定値から 16384 に上げている。

### 2. 読み出しのたびにDTRでNanoをリセットする必要がある

`nano2_master.ino` は `setup()` の中で `'R'` を送り、1バンク送ったら `loop()` は空、
という「使い切り」の設計になっている。
PC版(pyserial)が読み出しのたびに `serial.Serial()` でポートを開き直していたのは、
**その際のDTR操作でNanoがリセットされ `setup()` が再実行される**ことを利用していたため。

Androidではポートを開いたままにするので、明示的に `setDTR(false)→true` で
リセットパルスを送らないと、2回目以降の `'R'` が来ない。

### 3. Android 14以降のPendingIntent制限

USB権限リクエストで `Intent(ACTION_USB_PERMISSION)` に `FLAG_MUTABLE` を付けると、
Android 14(API 34)以降では暗黙的インテント扱いで例外になりアプリが落ちる。
`setPackage(packageName)` を付けて明示的インテントにする必要がある。

## 既知の制限・今後

- 読み出しのたびにNanoのリセット(ブートローダ起動待ち)が入るため、
  バンクあたり数秒のオーバーヘッドがある。これはPC版と同じ構造。
  ファーム側を `loop()` で繰り返し受け付ける形に改造すればリセット不要になり
  大幅に速くなるが、PC版との互換性に影響するため未着手。
- SRAM(セーブデータ)の読み出しはUI未実装。`CartReader.readBankOnce(sram=true)` は用意してある。
- CIC関連(`cic=true`、クロック分周指定)も同様に引数は用意済みだがUIからは未接続。
