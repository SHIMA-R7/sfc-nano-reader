// Nano-1: cart A0-A15 アドレス生成 ＋ /WR パルス出力
// STROBE(A4) の立ち上がりで16bitカウンタを+1し、A0-A15の16本に出力する。
// RESET(A5) の立ち上がりでカウンタを0に戻す。
//
// ポーリングでは取りこぼすのでピン変化割り込みを使う。
// ピン変化割り込みならエッジをハードウェアがラッチするので、
// 割り込みが少し遅れてもパルスを見逃さない。
//
// ■ ピン割り当てとポートの対応
//   A0-A5   -> D2-D7   = PORTD bit2-7
//   A6-A11  -> D8-D13  = PORTB bit0-5
//   A12-A15 -> A0-A3   = PORTC bit0-3
//   STROBE  -> A4      = PINC bit4 (PCINT12)
//   RESET   -> A5      = PINC bit5 (PCINT13)
//   **/WR   -> D1(PD1) = カート54番**        ← 2026-09-02 追加
//
// ■ なぜ D1 なのか
// Nano-1にはUSBを繋いでいないので D0/D1 が空いている。
// /WR はクロックではなく「書き込みの瞬間に1回LOWにする」だけの信号なので、
// タイマー出力を持たない PD1 でも足りる（PHI2はここでは作れない）。
//
// ■ 書き込み時の設定
//   Nano-1 は **atmega328old**（旧ブートローダ/57600bps）。2026-09-02 実測。
//   参考: Nano-2 は atmega328（新ブートローダ）、Nano-3 は atmega328old。
//     arduino-cli upload -p COM21 --fqbn arduino:avr:nano:cpu=atmega328old nano1_addr_low
//
// ■ **カセットを外してから書き込むこと**
// USBを挿すとブートローダがTXを喋る。/WR が繋がったままだと、そのビット列が
// そのまま書き込みパルスになる。**セーブデータが飛ぶ。** ソフトでは防げない。
//
// ■ 起動時にグリッチを出さない順序
//   PORTD |= _BV(PD1);   まず内蔵プルアップ。この時点で入力のまま弱くHIGH
//   DDRD  |= _BV(PD1);   それから出力。HIGHのまま切り替わる
// 逆順にすると、出力になった瞬間に一度LOWが出る。外付け抵抗の代わりになる。
//
// ■ 書き込みの指示をどう受けるか
// Nano-1はPCと通信できない。知っているのはSTROBEとRESETのパルスだけである。
// そこで**RESETのパルス幅**で意味を分ける。
//
//   **STROBE** の立ち上がり → 従来どおり addr++。その直後、STROBEがHIGHのままかを
//   ISR内で数え、長ければ /WR を1パルス出す。**アドレスは既に目的値になっている。**
//   RESETは従来どおり（手を入れない）。
//
//   書き込み手順:
//     RESET（短く）           addr = 0
//     STROBE × (A-1) 回（短く） addr = A-1
//     データバスに値を出す
//     STROBE × 1 回（長く）    addr = A になり、その状態で /WR が出る
//
//   **RESETの幅で判定する案は使えなかった。** リセット直後はアドレスが必ず0で、
//   アドレス0にしか書けない仕組みになる。
//
// 通常運用のRESETは digitalWrite の直後に戻すので数マイクロ秒しかない。
// 長いパルスは現れないので、取り違えない。
//
// ■ **一度ここで読み出しを壊した（2026-09-02）**
// 最初は「立ち上がりでは何もせず、立ち下がりで幅を見てリセットか/WRかを決める」
// 設計にした。アドレスを保ったまま書き込めるので綺麗だと思ったが、
// **リセットの実行が立ち下がりまで遅れる**ため、Nano-2がRESETを戻す前に
// STROBEを打つとアドレスがずれた。マリオRPGで **0/6**（変更前は10/20）。
//
// **既存の読み出しを壊さないことを最優先にする。**
// リセットは従来どおり立ち上がりで即実行し、/WR は立ち下がりで追加で出す。
// その結果 /WR パルスの前にカウンタは0になるので、
// **書き込み側はアドレスを毎回設定し直すこと。**

const uint8_t ADDR_PINS[16] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2, A3};
const uint8_t STROBE_PIN = A4;
const uint8_t RESET_PIN = A5;

#define STROBE_BIT _BV(PC4)
#define RESET_BIT  _BV(PC5)
#define WR_BIT     _BV(PD1)      // カート54番 /WR

// これ以上RESETがHIGHなら「書き込み指示」とみなす。
// 通常のRESETパルスは数us。50usなら誤認しない。
// ISR内でSTROBEのHIGH保持を数える回数。1周は数命令。
//   160回 ≒ 50us  … これ以上なら「書き込み指示」
//   640回 ≒ 200us … 上限。異常に長い場合に抜けるため
const uint16_t WR_COUNT_MIN = 160;
const uint16_t WR_COUNT_MAX = 640;
// /WR をLOWに保つ幅は pulseWr() のNOP数で決める（約4us）。
// SRAMの書き込みパルスは普通100ns未満で足りるが、経路にNanoが挟まるので余裕を取る。

volatile uint16_t addr = 0;

// 16bitシフト(v>>6, v>>12)はAVRだと重いので、上位/下位バイトに分けて8bit演算だけで組む。
static inline void writeAddr(uint16_t v) {
  const uint8_t lo = (uint8_t)v;
  const uint8_t hi = (uint8_t)(v >> 8);
  // PORTD bit0,1 は RX/TX、PORTB bit6,7 は水晶、PORTC bit4,5 は入力なので保存する
  // **bit1 は /WR なので絶対に壊さない。** マスクは 0x03 のままでよい。
  PORTD = (PORTD & 0x03) | (uint8_t)(lo << 2);                        // A0-A5
  PORTB = (PORTB & 0xC0) | (uint8_t)((lo >> 6) | ((hi & 0x0F) << 2)); // A6-A11
  PORTC = (PORTC & 0xF0) | (uint8_t)(hi >> 4);                        // A12-A15
}

// /WR を1回だけLOWにする。定数ビットへの |= / &= は sbi/cbi 1命令になるので
// リードモディファイライトの隙間が生まれない（Nano-3で踏んだ地雷への対策）。
static inline void pulseWr() {
  PORTD &= (uint8_t)~WR_BIT;
  // _delay_us は定数でないと展開されないので、NOPを数えて確実に幅を作る。
  // 16MHz = 1命令 62.5ns。64回で約4us。
  for (uint8_t i = 0; i < 64; i++) __asm__ __volatile__("nop");
  PORTD |= WR_BIT;
}

// PCINT1 は PORTC の変化でまとめて呼ばれる。立ち上がりと立ち下がりの両方を見る。
ISR(PCINT1_vect) {
  static uint8_t last = 0;
  const uint8_t now = PINC & (STROBE_BIT | RESET_BIT);
  const uint8_t rose = (uint8_t)(now & ~last);
  last = now;

  if (rose & RESET_BIT) {
    // リセットは従来どおり。**ここには一切手を入れない。**
    addr = 0;
    writeAddr(0);
  } else if (rose & STROBE_BIT) {
    writeAddr(++addr);
    // アドレスを更新した「あと」に、STROBEがHIGHのままかを数える。
    // 長ければ書き込み指示。**このときアドレスは既に目的値になっている。**
    // RESET幅で判定する案は、リセット直後なのでアドレスが必ず0になり使えなかった。
    uint16_t n = 0;
    while ((PINC & STROBE_BIT) && n < WR_COUNT_MAX) n++;
    if (n >= WR_COUNT_MIN) pulseWr();
  }
}

void setup() {
  // **/WR を最優先で安全側に倒す。** 順序を変えないこと。
  PORTD |= WR_BIT;                      // 内蔵プルアップ（まだ入力）
  DDRD  |= WR_BIT;                      // 出力へ。HIGHのまま切り替わる

  for (uint8_t i = 0; i < 16; i++) pinMode(ADDR_PINS[i], OUTPUT);
  pinMode(STROBE_PIN, INPUT);
  pinMode(RESET_PIN, INPUT);
  writeAddr(0);

  // millis()用のタイマ割り込みはISRの応答を遅らせるだけなので止める（delay/millis不使用）
  //
  // **ここを一度外して読み出しを壊した（2026-09-02）。**
  // RESETの幅を測るのに micros() を使いたくて TIMSK0=0 を消したところ、
  // マリオRPGが 0/6 になった（元は10/20）。
  // 「ISR内では割り込み禁止だから競合しない」と考えたのは誤りで、
  // **問題はISRの中ではなくISRが始まるまでの遅延**である。
  // Timer0のISRが実行中にSTROBEが来ると、その処理が終わるまでアドレス更新が待たされる。
  // 元のコメントは正しかった。理由を誤解して外した。
  TIMSK0 = 0;

  // PORTC のピン変化割り込みを A4/A5 だけ有効にする
  PCICR |= _BV(PCIE1);
  PCMSK1 = STROBE_BIT | RESET_BIT;
  sei();
}

void loop() {
  // 何もしない。すべて割り込みで処理する。
}
