// Nano-3: cart A16-A23（バンク）カウンタ
// Nano-1のアドレスカウンタと同じ「STROBEで+1、RESETで0」方式。
// 旧DATA/CLK/LATCHのシフトレジスタ方式は、2枚のNanoが非同期クロックで動くため
// タイミングのレースでビットを取りこぼし不安定だったので廃止した。
// 配線は変更していない。旧BANK_DATA線(D10)をRESET、旧BANK_CLK線(D11)をSTROBEとして使う。
// 旧BANK_LATCH線(D12)は未使用。

const uint8_t BANK_PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9}; // A16..A23
const uint8_t RESET_PIN = 10;
const uint8_t STROBE_PIN = 11;

uint8_t bank = 0;

void writeBank(uint8_t v) {
  for (uint8_t i = 0; i < 8; i++) {
    digitalWrite(BANK_PINS[i], (v >> i) & 1);
  }
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) pinMode(BANK_PINS[i], OUTPUT);
  pinMode(RESET_PIN, INPUT);
  pinMode(STROBE_PIN, INPUT);
  writeBank(0);
}

void loop() {
  static uint8_t lastReset = LOW;
  static uint8_t lastStrobe = LOW;

  uint8_t r = digitalRead(RESET_PIN);
  if (r == HIGH && lastReset == LOW) {
    bank = 0;
    writeBank(bank);
  }
  lastReset = r;

  uint8_t s = digitalRead(STROBE_PIN);
  if (s == HIGH && lastStrobe == LOW) {
    bank++;
    writeBank(bank);
  }
  lastStrobe = s;
}
