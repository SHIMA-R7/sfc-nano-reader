// Nano-2: バスサイクル制御 + データ読み取り + USBストリーム（案A: SDなし、PC直送）
//
// 手順（1バイトごと）:
//   1. /ROMSEL, /RD をLowにしてROMを選択・読み出しアサート
//   2. 数us待ってからD0-D7を読む
//   3. /RD, /ROMSELをHighに戻す
//   4. Serial.write()でPCへ送信
//   5. STROBEパルスでNano-1のアドレスを+1
//   6. 64KB(1バンク分)読み終えたらNano-3へ次のバンク値を送る
//
// 出荷時点でNUM_BANKSは仮値。実際のROMサイズが分かったら書き換えて再書き込みすること。
// (ヘッダ自動判定は未実装。まずは大きめの値で全部読み、PC側で余分を切り捨てる運用でも良い)

const uint8_t RD_PIN = 2;
const uint8_t ROMSEL_PIN = 3;
const uint8_t STROBE_PIN = 4;
const uint8_t BANK_DATA_PIN = 5;
const uint8_t BANK_CLK_PIN = 6;
const uint8_t BANK_LATCH_PIN = 7;
const uint8_t D6_PIN = 8;
const uint8_t D7_PIN = 9;
const uint8_t DATA_LOW_PINS[6] = {A0, A1, A2, A3, A4, A5}; // cart D0-D5
const uint8_t ADDR_RESET_PIN = 10; // Nano-1のアドレスカウンタを0に戻す
// D13は将来のSPI SD用に予約

#include <U8x8lib.h>
U8X8_SH1106_128X64_NONAME_SW_I2C oled(/*clock=*/ 12, /*data=*/ 11, /*reset=*/ U8X8_PIN_NONE);

const uint32_t NUM_BANKS = 32; // Super Puyo Puyo: 1MB = 32バンク x 32KB実質(64KBミラー込み)
const uint32_t BANK_SIZE = 65536UL;

void setDataPinsInput() {
  for (uint8_t i = 0; i < 6; i++) pinMode(DATA_LOW_PINS[i], INPUT);
  pinMode(D6_PIN, INPUT);
  pinMode(D7_PIN, INPUT);
}

uint8_t readDataBus() {
  uint8_t v = 0;
  for (uint8_t i = 0; i < 6; i++) {
    if (digitalRead(DATA_LOW_PINS[i])) v |= (1 << i);
  }
  if (digitalRead(D6_PIN)) v |= (1 << 6);
  if (digitalRead(D7_PIN)) v |= (1 << 7);
  return v;
}

void resetNano1Addr() {
  digitalWrite(ADDR_RESET_PIN, HIGH);
  delayMicroseconds(50);
  digitalWrite(ADDR_RESET_PIN, LOW);
  delayMicroseconds(100);
}

void pulseStrobe() {
  digitalWrite(STROBE_PIN, HIGH);
  delayMicroseconds(50);
  digitalWrite(STROBE_PIN, LOW);
  delayMicroseconds(100); // Nano-1側の16本分のdigitalWrite完了を待つマージン
}

// bank値をMSBファーストでNano-3へシフトし、LATCHで確定させる
void sendBankOnce(uint8_t bank) {
  for (int8_t i = 7; i >= 0; i--) {
    digitalWrite(BANK_DATA_PIN, (bank >> i) & 1);
    delayMicroseconds(200); // DATAセットアップ時間
    digitalWrite(BANK_CLK_PIN, HIGH);
    delayMicroseconds(300); // Nano-3側のdigitalRead()ポーリング周期に対して十分なマージン
    digitalWrite(BANK_CLK_PIN, LOW);
    delayMicroseconds(300);
  }
  digitalWrite(BANK_LATCH_PIN, HIGH);
  delayMicroseconds(300);
  digitalWrite(BANK_LATCH_PIN, LOW);
  delayMicroseconds(300);
}

// 非同期な2台間のタイミングレースで取りこぼす事故を減らすため、念のため2回送る
void sendBank(uint8_t bank) {
  sendBankOnce(bank);
  delayMicroseconds(500);
  sendBankOnce(bank);
}

void splashScreen() {
  oled.clear();
  oled.setFont(u8x8_font_chroma48medium8_r);

  // 枠線
  oled.draw2x2String(1, 1, "SFC");
  oled.draw2x2String(1, 4, "DUMP");
  oled.drawString(2, 7, "Nano x3 Reader");

  delay(600);

  // ローディングバー風アニメーション
  char bar[17] = "[              ]";
  for (uint8_t i = 0; i < 14; i++) {
    bar[i + 1] = '#';
    oled.drawString(0, 6, bar);
    delay(70);
  }
  delay(300);

  // 起動完了フラッシュ演出
  for (uint8_t i = 0; i < 3; i++) {
    oled.setInverseFont(1);
    oled.drawString(2, 7, "Nano x3 Reader");
    delay(120);
    oled.setInverseFont(0);
    oled.drawString(2, 7, "Nano x3 Reader");
    delay(120);
  }

  delay(400);
  oled.clear();
}

uint8_t readByte() {
  digitalWrite(ROMSEL_PIN, LOW);
  digitalWrite(RD_PIN, LOW);
  delayMicroseconds(100); // 長い配線の寄生容量を考慮して大きめに確保
  uint8_t v = readDataBus();
  digitalWrite(RD_PIN, HIGH);
  digitalWrite(ROMSEL_PIN, HIGH);
  return v;
}

void setup() {
  pinMode(RD_PIN, OUTPUT); digitalWrite(RD_PIN, HIGH);
  pinMode(ROMSEL_PIN, OUTPUT); digitalWrite(ROMSEL_PIN, HIGH);
  pinMode(STROBE_PIN, OUTPUT); digitalWrite(STROBE_PIN, LOW);
  pinMode(BANK_DATA_PIN, OUTPUT); digitalWrite(BANK_DATA_PIN, LOW);
  pinMode(BANK_CLK_PIN, OUTPUT); digitalWrite(BANK_CLK_PIN, LOW);
  pinMode(BANK_LATCH_PIN, OUTPUT); digitalWrite(BANK_LATCH_PIN, LOW);
  pinMode(ADDR_RESET_PIN, OUTPUT); digitalWrite(ADDR_RESET_PIN, LOW);
  setDataPinsInput();

  oled.begin();
  splashScreen();
  oled.setFont(u8x8_font_chroma48medium8_r);
  oled.drawString(0, 0, "SFC DUMPER");
  oled.drawString(0, 2, "waiting PC...");

  Serial.begin(250000);
  delay(3000); // PC側スクリプトがポートを開いて受信準備するまでの猶予
  resetNano1Addr();
  sendBank(0);

  char line[17];
  for (uint32_t bank = 0; bank < NUM_BANKS; bank++) {
    if (bank > 0) sendBank((uint8_t)bank);
    snprintf(line, sizeof(line), "Bank %2lu/%2lu", (unsigned long)(bank + 1), (unsigned long)NUM_BANKS);
    oled.drawString(0, 2, line);
    for (uint32_t a = 0; a < BANK_SIZE; a++) {
      uint8_t b = readByte();
      Serial.write(b);
      pulseStrobe(); // 読んだ直後に次のアドレスへ進める
    }
  }

  oled.drawString(0, 2, "DONE          ");

  while (1) {
    // 完了。ここで停止し続ける
  }
}

void loop() {}
