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

// PC側がシリアルポートを開くたびDTRでリセットがかかるため、放っておくと
// 1バンクごとに起動演出が流れる（64バンク×2回=128回）。
// SRAMの内容はリセットでは保持され、電源を切ると失われる。この性質を使って
// 「電源を入れた最初の1回」だけ演出を出す。.noinit なので初期化されない。
__attribute__((section(".noinit"))) uint32_t bootMagic;
const uint32_t BOOT_MAGIC = 0x5FC0DEADUL;

// 読み出しタイミングはPC側から毎回指定される（コンパイル時固定ではない）。
// 理由: 必要なマージンはカートのROMチップ個体差でかなり違う。同じ5usの設定で
// Super Mario Collection と 夜光虫 は一発で通ったが、Street Fighter II は
// 2回連続一致に何度失敗しても揃わず、100usまで上げてようやく安定した。
// 全カート一律で遅くするのは、マージンが要らないカートの速度を無駄に捨てることになる。
// そこでPC側が「まず5usで試し、駄目なら段階的に上げる」エスカレーションを行い、
// 都度この変数へ値をセットしてから読む。
uint16_t rdSettleUs = 5;
uint16_t addrSettleUs = 5;
uint16_t pulseUs = 3;

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
  delayMicroseconds(pulseUs);
  digitalWrite(ADDR_RESET_PIN, LOW);
  delayMicroseconds(100);
}

void pulseStrobe() {
  digitalWrite(STROBE_PIN, HIGH);
  delayMicroseconds(pulseUs);
  digitalWrite(STROBE_PIN, LOW);
  delayMicroseconds(addrSettleUs); // Nano-1側の16本分のdigitalWrite完了を待つマージン
}

void resetNano3Bank() {
  digitalWrite(BANK_RESET_PIN, HIGH);
  delayMicroseconds(pulseUs);
  digitalWrite(BANK_RESET_PIN, LOW);
  delayMicroseconds(100);
}

// Nano-1のpulseStrobe()と同じ発想: このパルス1回でNano-3のバンクが+1される
void pulseBankStrobe() {
  digitalWrite(BANK_STROBE_PIN, HIGH);
  delayMicroseconds(pulseUs);
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
  delayMicroseconds(rdSettleUs); // 長い配線の寄生容量を考慮して大きめに確保
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
  const bool coldBoot = (bootMagic != BOOT_MAGIC);
  bootMagic = BOOT_MAGIC;
  if (coldBoot) {
    splashScreen();   // 電源投入時のみ
  } else {
    oled.clear();     // リセット時は静かに立ち上げる
  }
  oled.setFont(u8x8_font_chroma48medium8_r);
  oled.drawString(0, 0, "SFC DUMPER");
  oled.drawString(0, 2, "waiting PC...");

  Serial.begin(1000000); // 16MHzなら誤差0%
  delay(50);

  // PC側から「バンク番号(1B) + 全バンク数(1B) + RD待ち(2B,LE) + ADDR待ち(2B,LE)
  // + パルス幅(2B,LE)」の計8バイトを受け取る。タイミングはコンパイル時固定ではなく、
  // PC側がカートごとに「まず速い設定で試し、駄目なら段階的に上げる」ために毎回送ってくる。
  // 全バンク数はOLEDに「現在/全体」を表示するためだけに使う（読み出し自体には無関係）。
  // 受信準備ができたことを 'R' で知らせてから待つ（先に送られると取りこぼすため）。
  oled.drawString(0, 2, "waiting bank..");
  Serial.write('R');
  uint8_t hdr[8];
  for (uint8_t i = 0; i < 8; i++) {
    while (Serial.available() == 0) { /* 待機 */ }
    hdr[i] = (uint8_t)Serial.read();
  }
  uint8_t target = hdr[0];
  uint8_t totalBanks = hdr[1];
  rdSettleUs   = (uint16_t)(hdr[2] | (hdr[3] << 8));
  addrSettleUs = (uint16_t)(hdr[4] | (hdr[5] << 8));
  pulseUs      = (uint16_t)(hdr[6] | (hdr[7] << 8));

  char line[17];
  if (totalBanks > 0) {
    snprintf(line, sizeof(line), "Bank %3u/%3u  ", (unsigned)target + 1, (unsigned)totalBanks);
  } else {
    snprintf(line, sizeof(line), "Bank %3u      ", (unsigned)target);
  }
  oled.drawString(0, 2, line);

  resetNano1Addr();
  resetNano3Bank();
  for (uint16_t b = 0; b < target; b++) pulseBankStrobe(); // 目的のバンクまで進める

  char prog[17];
  for (uint32_t a = 0; a < BANK_SIZE; a++) {
    uint8_t v = readByte();
    Serial.write(v);
    pulseStrobe(); // 読んだ直後に次のアドレスへ進める

    // OLED更新は1回数msかかるので毎バイトはやらない（47us/byteの律速を壊す）。
    // 8192バイトごと(1バンクあたり8回)なら誤差程度。
    if ((a & 0x1FFF) == 0) {
      snprintf(prog, sizeof(prog), "%5lu/%5lu", (unsigned long)(a + 1), (unsigned long)BANK_SIZE);
      oled.drawString(0, 4, prog);
    }
  }
  snprintf(prog, sizeof(prog), "%5lu/%5lu", (unsigned long)BANK_SIZE, (unsigned long)BANK_SIZE);
  oled.drawString(0, 4, prog);

  oled.drawString(0, 2, "DONE          ");

  while (1) {
    // 完了。ここで停止し続ける
  }
}

void loop() {}
