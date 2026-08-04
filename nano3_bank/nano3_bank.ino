// Nano-3: cart A16-A23（バンク）ラッチ
// Nano-2から DATA/CLK/LATCH の3線シリアルで8bit値を受け取り、
// A16-A23の8本に出力する（ソフトウェア版74HC595のような動作）。
// MSBファーストで受信する想定（Nano-2側と対で変更しないこと）。

const uint8_t BANK_PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9}; // A16..A23
const uint8_t DATA_PIN = 10;
const uint8_t CLK_PIN = 11;
const uint8_t LATCH_PIN = 12;

uint8_t shiftReg = 0;

void writeBank(uint8_t v) {
  for (uint8_t i = 0; i < 8; i++) {
    digitalWrite(BANK_PINS[i], (v >> i) & 1);
  }
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) pinMode(BANK_PINS[i], OUTPUT);
  pinMode(DATA_PIN, INPUT);
  pinMode(CLK_PIN, INPUT);
  pinMode(LATCH_PIN, INPUT);
  writeBank(0);
}

void loop() {
  static uint8_t lastClk = LOW;
  static uint8_t lastLatch = LOW;

  uint8_t clk = digitalRead(CLK_PIN);
  if (clk == HIGH && lastClk == LOW) {
    shiftReg = (uint8_t)((shiftReg << 1) | digitalRead(DATA_PIN));
  }
  lastClk = clk;

  uint8_t latch = digitalRead(LATCH_PIN);
  if (latch == HIGH && lastLatch == LOW) {
    writeBank(shiftReg);
  }
  lastLatch = latch;
}
