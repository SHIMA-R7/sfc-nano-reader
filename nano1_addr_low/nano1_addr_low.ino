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
//   短い(<WR_MIN_US) RESET → 従来どおりカウンタを0に戻す
//   長い(>=WR_MIN_US) RESET → **カウンタは触らず、/WR を1パルス出す**
//
// 通常運用のRESETは digitalWrite の直後に戻すので数マイクロ秒しかない。
// 長いパルスは現れないので、取り違えない。
// **アドレスを保ったまま書き込みを指示できる**のが要点で、
// 「RESETを保持したままSTROBE」方式だとカウンタが0に戻ってしまい使えなかった。

const uint8_t ADDR_PINS[16] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2, A3};
const uint8_t STROBE_PIN = A4;
const uint8_t RESET_PIN = A5;

#define STROBE_BIT _BV(PC4)
#define RESET_BIT  _BV(PC5)
#define WR_BIT     _BV(PD1)      // カート54番 /WR

// これ以上RESETがHIGHなら「書き込み指示」とみなす。
// 通常のRESETパルスは数us。50usなら誤認しない。
const uint16_t WR_MIN_US = 50;
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
  static uint16_t resetHighAt = 0;     // RESETが立ち上がった時刻(us)
  const uint8_t now = PINC & (STROBE_BIT | RESET_BIT);
  const uint8_t rose = (uint8_t)(now & ~last);
  const uint8_t fell = (uint8_t)(~now & last);
  last = now;

  if (rose & RESET_BIT) {
    resetHighAt = (uint16_t)micros();   // まだ何もしない。幅を見てから決める
  } else if (fell & RESET_BIT) {
    const uint16_t held = (uint16_t)micros() - resetHighAt;
    if (held >= WR_MIN_US) {
      pulseWr();                        // 長い = 書き込み指示。**アドレスは触らない**
    } else {
      addr = 0;                         // 短い = 従来どおりのリセット
      writeAddr(0);
    }
  } else if (rose & STROBE_BIT) {
    writeAddr(++addr);
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

  // millis()用のタイマ割り込みはISRの応答を遅らせるだけなので止める…
  // **としていたが、micros() を使うようになったので Timer0 は生かす。**
  // ISRの中で micros() を呼ぶが、Timer0のオーバーフロー割り込みは
  // PCINT1より優先度が低く、ISR内では割り込み禁止なので競合しない。
  // 桁上がりを1回取りこぼす可能性はあるが、判定は50us対数usの粗い比較なので影響しない。

  // PORTC のピン変化割り込みを A4/A5 だけ有効にする
  PCICR |= _BV(PCIE1);
  PCMSK1 = STROBE_BIT | RESET_BIT;
  sei();
}

void loop() {
  // 何もしない。すべて割り込みで処理する。
}
