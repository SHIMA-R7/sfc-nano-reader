// Nano-1: cart A0-A15 アドレス生成
// STROBE(A4) の立ち上がりで16bitカウンタを+1し、A0-A15の16本に出力する。
// 0xFFFF -> 0x0000 は自然にオーバーフローするので、
// 64KBごとにNano-2がバンクを切り替えるタイミングと自動的に一致する。

const uint8_t ADDR_PINS[16] = {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, A0, A1, A2, A3};
const uint8_t STROBE_PIN = A4;
const uint8_t RESET_PIN = A5; // Nano-2から: 立ち上がりでaddrを強制的に0へ

uint16_t addr = 0;

void writeAddr(uint16_t v) {
  for (uint8_t i = 0; i < 16; i++) {
    digitalWrite(ADDR_PINS[i], (v >> i) & 1);
  }
}

void setup() {
  for (uint8_t i = 0; i < 16; i++) pinMode(ADDR_PINS[i], OUTPUT);
  pinMode(STROBE_PIN, INPUT);
  pinMode(RESET_PIN, INPUT);
  writeAddr(0);
}

void loop() {
  static uint8_t lastStrobe = LOW;
  static uint8_t lastReset = LOW;

  uint8_t r = digitalRead(RESET_PIN);
  if (r == HIGH && lastReset == LOW) {
    addr = 0;
    writeAddr(addr);
  }
  lastReset = r;

  uint8_t s = digitalRead(STROBE_PIN);
  if (s == HIGH && lastStrobe == LOW) {
    addr++;
    writeAddr(addr);
  }
  lastStrobe = s;
}
