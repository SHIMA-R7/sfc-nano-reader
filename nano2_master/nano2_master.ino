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

// バンク単位モード:
//   長時間連続で読み続けると読み取りが化ける（同じバンクでも全体ダンプ中は毎回700バイト前後
//   誤るのに、単独で読めば2回とも完全一致する）。そこで1回の起動につき1バンクだけ読む。
//   PC側が起動ごとに「読みたいバンク番号」を1バイト送ってくる。
const uint32_t BANK_SIZE = 65536UL;

// 読み出しタイミングの実験用パラメータ
const uint16_t RD_SETTLE_US = 5;   // /RDアサート後にデータを読むまでの待ち
const uint16_t ADDR_SETTLE_US = 5; // アドレス更新後の待ち
const uint16_t PULSE_US = 3;        // STROBEパルス幅。相手はPCINTなので短くてよい

void setDataPinsInput() {
  for (uint8_t i = 0; i < 6; i++) pinMode(DATA_LOW_PINS[i], INPUT);
  pinMode(D6_PIN, INPUT);
  pinMode(D7_PIN, INPUT);
}

// cart D0-D5 -> A0-A5 = PINC bit0-5 / cart D6-D7 -> D8-D9 = PINB bit0-1
static inline uint8_t readDataBus() {
  return (uint8_t)((PINC & 0x3F) | ((PINB & 0x03) << 6));
}

void resetNano1Addr() {
  digitalWrite(ADDR_RESET_PIN, HIGH);
  delayMicroseconds(PULSE_US);
  digitalWrite(ADDR_RESET_PIN, LOW);
  delayMicroseconds(100);
}

void pulseStrobe() {
  digitalWrite(STROBE_PIN, HIGH);
  delayMicroseconds(PULSE_US);
  digitalWrite(STROBE_PIN, LOW);
  delayMicroseconds(ADDR_SETTLE_US); // Nano-1側の16本分のdigitalWrite完了を待つマージン
}

void resetNano3Bank() {
  digitalWrite(BANK_RESET_PIN, HIGH);
  delayMicroseconds(PULSE_US);
  digitalWrite(BANK_RESET_PIN, LOW);
  delayMicroseconds(100);
}

// Nano-1のpulseStrobe()と同じ発想: このパルス1回でNano-3のバンクが+1される
void pulseBankStrobe() {
  digitalWrite(BANK_STROBE_PIN, HIGH);
  delayMicroseconds(PULSE_US);
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
  delayMicroseconds(RD_SETTLE_US); // 長い配線の寄生容量を考慮して大きめに確保
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

  Serial.begin(1000000); // 16MHzなら誤差0%
  delay(50);

  // PC側から「読みたいバンク番号」を1バイト受け取る。
  // 受信準備ができたことを 'R' で知らせてから待つ（先に送られると取りこぼすため）。
  oled.drawString(0, 2, "waiting bank..");
  Serial.write('R');
  while (Serial.available() == 0) { /* 待機 */ }
  uint8_t target = (uint8_t)Serial.read();

  char line[17];
  snprintf(line, sizeof(line), "Bank %3u      ", (unsigned)target);
  oled.drawString(0, 2, line);

  resetNano1Addr();
  resetNano3Bank();
  for (uint16_t b = 0; b < target; b++) pulseBankStrobe(); // 目的のバンクまで進める

  for (uint32_t a = 0; a < BANK_SIZE; a++) {
    uint8_t v = readByte();
    Serial.write(v);
    pulseStrobe(); // 読んだ直後に次のアドレスへ進める
  }

  oled.drawString(0, 2, "DONE          ");

  while (1) {
    // 完了。ここで停止し続ける
  }
}

void loop() {}
