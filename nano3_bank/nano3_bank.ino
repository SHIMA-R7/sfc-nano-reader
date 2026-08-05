// Nano-3: cart A16-A23（バンク）カウンタ
// Nano-1のアドレスカウンタと同じ「STROBEで+1、RESETで0」方式。
// 旧DATA/CLK/LATCHのシフトレジスタ方式は、2枚のNanoが非同期クロックで動くため
// タイミングのレースでビットを取りこぼし不安定だったので廃止した。
// 配線は変更していない。旧BANK_DATA線(D10)をRESET、旧BANK_CLK線(D11)をSTROBEとして使う。
// 旧BANK_LATCH線(D12)は未使用。
//
// Nano-1と同じ理由でピン変化割り込みを使う（ポーリングだとパルスを取りこぼす）。
//
// ピン割り当てとポートの対応:
//   A16-A21 -> D2-D7  = PORTD bit2-7
//   A22-A23 -> D8-D9  = PORTB bit0-1
//   RESET   -> D10    = PINB bit2 (PCINT2)
//   STROBE  -> D11    = PINB bit3 (PCINT3)

const uint8_t BANK_PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9}; // A16..A23
const uint8_t RESET_PIN = 10;
const uint8_t STROBE_PIN = 11;

#define RESET_BIT  _BV(PB2)
#define STROBE_BIT _BV(PB3)

volatile uint8_t bank = 0;

static inline void writeBank(uint8_t v) {
  // PORTD bit0,1 は RX/TX、PORTB bit2,3 は入力、bit6,7 は水晶なので保存する
  PORTD = (PORTD & 0x03) | (uint8_t)((v & 0x3F) << 2);
  PORTB = (PORTB & 0xFC) | (uint8_t)((v >> 6) & 0x03);
}

// PCINT0 は PORTB の変化でまとめて呼ばれる。立ち上がりだけを拾う。
ISR(PCINT0_vect) {
  static uint8_t last = 0;
  const uint8_t now = PINB & (STROBE_BIT | RESET_BIT);
  const uint8_t rose = (uint8_t)(now & ~last);
  last = now;

  if (rose & RESET_BIT) {
    bank = 0;
    writeBank(0);
  } else if (rose & STROBE_BIT) {
    writeBank(++bank);
  }
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) pinMode(BANK_PINS[i], OUTPUT);
  pinMode(RESET_PIN, INPUT);
  pinMode(STROBE_PIN, INPUT);
  writeBank(0);

  TIMSK0 = 0; // millis()用のタイマ割り込みを止める（delay/millis不使用）

  PCICR |= _BV(PCIE0);
  PCMSK0 = STROBE_BIT | RESET_BIT;
  sei();
}

void loop() {
  // 何もしない。すべて割り込みで処理する。
}
