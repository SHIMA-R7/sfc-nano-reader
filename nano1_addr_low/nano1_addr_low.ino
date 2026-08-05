// Nano-1: cart A0-A15 アドレス生成
// STROBE(A4) の立ち上がりで16bitカウンタを+1し、A0-A15の16本に出力する。
// RESET(A5) の立ち上がりでカウンタを0に戻す。
// 0xFFFF -> 0x0000 は自然にオーバーフローするので、
// 64KBごとにNano-2がバンクを切り替えるタイミングと自動的に一致する。
//
// ■ なぜ割り込みなのか
// 最初はloop()でPINCをポーリングしていたが、これは「ループを一周する間に来たパルスを
// 取りこぼす」ことが原理的に避けられない。実際、Nano-2を高速化したら
// **きっかり1回おきに取りこぼし**、同じアドレスを2回ずつ読む症状が出た
// （出力[i] == 正解[i/2] が100%一致することで確定）。
// ピン変化割り込みならエッジをハードウェアがラッチするので、
// ISRが動く前にパルスが終わっていても取りこぼさない。
//
// ■ ピン割り当てとポートの対応
//   A0-A5   -> D2-D7   = PORTD bit2-7
//   A6-A11  -> D8-D13  = PORTB bit0-5
//   A12-A15 -> A0-A3   = PORTC bit0-3
//   STROBE  -> A4      = PINC bit4 (PCINT12)
//   RESET   -> A5      = PINC bit5 (PCINT13)

const uint8_t ADDR_PINS[16] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2, A3};
const uint8_t STROBE_PIN = A4;
const uint8_t RESET_PIN = A5;

#define STROBE_BIT _BV(PC4)
#define RESET_BIT  _BV(PC5)

volatile uint16_t addr = 0;

// 16bitシフト(v>>6, v>>12)はAVRだと重いので、上位/下位バイトに分けて8bit演算だけで組む。
static inline void writeAddr(uint16_t v) {
  const uint8_t lo = (uint8_t)v;
  const uint8_t hi = (uint8_t)(v >> 8);
  // PORTD bit0,1 は RX/TX、PORTB bit6,7 は水晶、PORTC bit4,5 は入力なので保存する
  PORTD = (PORTD & 0x03) | (uint8_t)(lo << 2);                        // A0-A5
  PORTB = (PORTB & 0xC0) | (uint8_t)((lo >> 6) | ((hi & 0x0F) << 2)); // A6-A11
  PORTC = (PORTC & 0xF0) | (uint8_t)(hi >> 4);                        // A12-A15
}

// PCINT1 は PORTC の変化でまとめて呼ばれる。立ち上がりだけを拾う。
ISR(PCINT1_vect) {
  static uint8_t last = 0;
  const uint8_t now = PINC & (STROBE_BIT | RESET_BIT);
  const uint8_t rose = (uint8_t)(now & ~last);
  last = now;

  if (rose & RESET_BIT) {
    addr = 0;
    writeAddr(0);
  } else if (rose & STROBE_BIT) {
    writeAddr(++addr);
  }
}

void setup() {
  for (uint8_t i = 0; i < 16; i++) pinMode(ADDR_PINS[i], OUTPUT);
  pinMode(STROBE_PIN, INPUT);
  pinMode(RESET_PIN, INPUT);
  writeAddr(0);

  // millis()用のタイマ割り込みはISRの応答を遅らせるだけなので止める（delay/millis不使用）
  TIMSK0 = 0;

  // PORTC のピン変化割り込みを A4/A5 だけ有効にする
  PCICR |= _BV(PCIE1);
  PCMSK1 = STROBE_BIT | RESET_BIT;
  sei();
}

void loop() {
  // 何もしない。すべて割り込みで処理する。
}
