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
// 旧DATA/CLK/LATCHのシフトレジスタ方式は非同期タイミングのレースで不安定だったため廃止。
// 配線はそのまま流用し、旧BANK_DATA線をRESET、旧BANK_CLK線をSTROBEとして使う(Nano-1と同じ方式)。
// 旧BANK_LATCH線(D7→Nano-3 D12)は未使用のまま放置。
const uint8_t BANK_RESET_PIN = 5;
const uint8_t BANK_STROBE_PIN = 6;
const uint8_t D6_PIN = 8;
const uint8_t D7_PIN = 9;
const uint8_t DATA_LOW_PINS[6] = {A0, A1, A2, A3, A4, A5}; // cart D0-D5
const uint8_t ADDR_RESET_PIN = 10; // Nano-1のアドレスカウンタを0に戻す
// D13は将来のSPI SD用に予約

#include <U8x8lib.h>
U8X8_SH1106_128X64_NONAME_SW_I2C oled(/*clock=*/ 12, /*data=*/ 11, /*reset=*/ U8X8_PIN_NONE);

const uint32_t NUM_BANKS = 32; // Super Mario Collection: HiROM 2MB = 32バンク x 64KB(ミラーなし、フル使用)
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

void resetNano3Bank() {
  digitalWrite(BANK_RESET_PIN, HIGH);
  delayMicroseconds(50);
  digitalWrite(BANK_RESET_PIN, LOW);
  delayMicroseconds(100);
}

// Nano-1のpulseStrobe()と同じ発想: このパルス1回でNano-3のバンクが+1される
void pulseBankStrobe() {
  digitalWrite(BANK_STROBE_PIN, HIGH);
  delayMicroseconds(50);
  digitalWrite(BANK_STROBE_PIN, LOW);
  delayMicroseconds(100);
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
  pinMode(BANK_RESET_PIN, OUTPUT); digitalWrite(BANK_RESET_PIN, LOW);
  pinMode(BANK_STROBE_PIN, OUTPUT); digitalWrite(BANK_STROBE_PIN, LOW);
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
  resetNano3Bank();

  char line[17];
  for (uint32_t bank = 0; bank < NUM_BANKS; bank++) {
    if (bank > 0) pulseBankStrobe(); // bankは常に+1ずつ進むのでパルス1回で足りる
    // 検証済み: OLEDの更新は読み取り誤りの原因ではない（無効にしても誤り率は変わらなかった）。
    // 逆に更新後にdelayを入れると悪化したので、素直にその場で描画する。
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
