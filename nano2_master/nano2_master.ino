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
const uint8_t BANK_RESET_PIN = 5;
const uint8_t BANK_STROBE_PIN = 6;
// 旧BANK_LATCH線(D7)はNano-3のD12から切り離し、カートの /RESET へ引き直した。
// カート側で +5V に直結していた線は撤去済み(残したままここをLowにすると電源とショートする)。
// 通常はHighに保つ。コプロ(Super FX / SA-1)をリセット状態に保持してROMを覗きたいときだけLow。
const uint8_t CART_RESET_PIN = 7;
const uint8_t D6_PIN = 8;
const uint8_t D7_PIN = 9;
const uint8_t DATA_LOW_PINS[6] = {A0, A1, A2, A3, A4, A5}; // cart D0-D5
const uint8_t ADDR_RESET_PIN = 10; // Nano-1のアドレスカウンタを0に戻す

// カートへ供給するクロック(PHI2 / カート57番)。
// マスクROMは非同期なので不要だが、コプロ搭載カートは中のチップが同期回路なので、
// クロックが来ないとリセットすら伝わらず、ROMを正しく通してくれない。
// Super FXのスターフォックスで、タイミングを変えても電源を強化しても /RESET を叩いても
// 約920バイト/バンクが0xFFのまま残ったのが、この仮説に至った経緯。
//
// D11でなければならない理由: 1MHzはソフトでは出せずタイマーのハードウェア出力が要る。
// Nano-2で解放できるタイマー出力ピンはOC2A(=D11)だけ。そのためOLEDのデータ線をD13へ
// 追い出してD11を空けた。実機の21.477MHzである必要はなく、OSCRもカートを1MHzで走らせている。
const uint8_t CART_CLK_PIN = 11;

// OLEDは休止中。D11をカートのクロック出力に明け渡した際、データ線をD13へ移したが、
// D13にはNano基板上のLEDと抵抗がぶら下がっていて駆動が足りず表示できなくなった。
// Nano-2に他の空きピンは無いため、進捗表示を諦めてD12/D13をCIC制御へ回す。
// 1に戻せば元の配線(clock=12,data=11)で復活するが、その場合カートクロックは使えない。
#define USE_OLED 0

#if USE_OLED
#include <U8x8lib.h>
U8X8_SH1106_128X64_NONAME_SW_I2C oled(/*clock=*/ 12, /*data=*/ 11, /*reset=*/ U8X8_PIN_NONE);
#else
// 呼び出し側を #if で汚さずに済ませるための何もしない代替。
static const uint8_t *u8x8_font_chroma48medium8_r = nullptr;
struct OledStub {
  void begin() {}
  void clear() {}
  void setFont(const uint8_t *) {}
  void drawString(uint8_t, uint8_t, const char *) {}
  void draw2x2String(uint8_t, uint8_t, const char *) {}
  void setInverseFont(uint8_t) {}
} oled;
#endif

// ── CIC(F411A)の制御 ────────────────────────────────────────────────
// SA-1はROMを開示する前に、自分のCICを介して本物の本体と通信していることを検証する。
// 本体基板から取り外したF411Aを繋ぎ、その検証相手を務めさせるための配線。
//
//   CIC 8番(RST)  <- D12    リセットを解除すると認証が始まる
//   CIC 10番(/RESET出力) -> A6   認証に失敗している間Lowのまま
//   CIC 7番(CLK)  <- D11    カートの56番/57番と同じクロックを分岐して供給
//
// A6を使うのはNanoの入力専用ピンだから。出力に使えず今まで遊んでいた。
// D13を避けるのは上記のLEDのため。監視線に余計な負荷を掛けたくない。
const uint8_t CIC_RST_PIN = 12;
const uint8_t CIC_OK_PIN = A6;   // analogRead専用。digitalReadは使えない
// A7は用途を付け替えながら使う唯一の観測窓。今はCICのデータ線(1番)を見ている。
// 11番(カート側CICのリセット)を見たときは、1MHz以下でHigh固定＝Key側は解除されて
// 動いていることが確認できた。次に知りたいのは「両者が実際に喋っているか」なので、
// データ線に移した。振動していれば通信は起きていて中身が合わないだけ、静止していれば
// どちらかが応答していない。
const uint8_t CIC_PROBE_PIN = A7;

// 16MHz / (2 * (1 + OCR2A)) = 出力周波数。7で1MHz、1で4MHz、0で8MHz。
void startCartClock(uint8_t ocr) {
  pinMode(CART_CLK_PIN, OUTPUT);
  TCCR2A = _BV(COM2A0) | _BV(WGM21); // CTCモード、比較一致でOC2Aをトグル
  TCCR2B = _BV(CS20);                // 分周なし
  OCR2A = ocr;
}

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

// セーブ用SRAMを読むときは /ROMSEL をアサートしてはいけない。
// ROMは /ROMSEL で選択されるが、カート上のSRAMはバンク($70〜 / $20〜)とアドレスを
// カート内部のデコード回路が見て自前で選択する。ROM読み出しと同じように /ROMSEL を
// Lowにすると、SRAMではなくROM側が応答してしまう。
//
// なお /WR はカート側で +5V に直結してあるので、ここで何をしようとSRAMへの書き込みは
// 物理的に起こらない。救出対象のセーブデータを壊す事故は原理的に発生しない。
bool sramMode = false;

uint8_t readByte() {
  if (!sramMode) digitalWrite(ROMSEL_PIN, LOW);
  digitalWrite(RD_PIN, LOW);
  delayMicroseconds(rdSettleUs); // 長い配線の寄生容量を考慮して大きめに確保
  uint8_t v = readDataBus();
  digitalWrite(RD_PIN, HIGH);
  if (!sramMode) digitalWrite(ROMSEL_PIN, HIGH);
  return v;
}

// CICを起動して認証の成立を待つ。戻り値は成立したかどうか。
//
// 10番ピンは本体のリセット線を駆動する出力で、CICは認証が通らない限りここをLowに
// 保持し続ける。つまり**ROMを読む前に握手の成否だけを切り分けられる**。
// これが無いと「読めない」という結果だけを見て、認証・配線・クロックのどれが悪いのか
// 区別できなくなる。
// 監視ピンの実測値の範囲も返す。'成立/不成立'の一言だけだと、A6がCICを本当に読めて
// いるのか(繋がっていて常にLow)、それとも浮いてノイズを拾っているだけなのかが分からない。
// 実測の最小値・最大値が分かれば、固定Low・固定High・不安定のどれかを判別できる。
// リセットを解除したあと、監視ピンが実際に何をしているかを1秒間観測して返す。
//
// 二値の「成立/不成立」を判定条件で決めようとして三度失敗した(2.5V閾値・4.4V瞬時・
// 200ms保持)。いずれも失敗モード側が条件を満たせてしまう。CICは認証に失敗すると本体を
// リセットし続けるので出力は振動し、クロックを落とすほどその周期が伸びて、どんな
// 「一定時間High」条件もいつかは通過してしまう。
//
// なので判定をやめて波形を測る。認証が通ったならリセットは恒久的に解除され、遷移は
// 起きないはず。失敗しているなら往復が観測される。
// Arduinoのプリプロセッサは構造体定義より前に関数プロトタイプを差し込むため、
// 自作型を引数に取るとコンパイルが通らない。素直にグローバルで受け渡す。
uint8_t obsKeyLo, obsKeyHi, obsKeyTransitions;  // 11番(カート側CICのリセット)の観測
uint8_t obsLo, obsHi;        // 観測した電圧の範囲(0-255に丸め)
uint8_t obsTransitions;      // Low↔Highの遷移回数。0でHigh維持なら本物
bool obsEndedHigh;

static void cicObserve(bool activeLow, uint16_t windowMs) {
  pinMode(CIC_RST_PIN, OUTPUT);
  digitalWrite(CIC_RST_PIN, activeLow ? LOW : HIGH);
  delay(20);
  digitalWrite(CIC_RST_PIN, activeLow ? HIGH : LOW);

  uint16_t mn = 1023, mx = 0, kmn = 1023, kmx = 0;
  uint8_t tr = 0, ktr = 0;
  bool prevHigh = false, kPrevHigh = false, first = true;
  const uint32_t deadline = millis() + windowMs;
  while (millis() < deadline) {
    const uint16_t v = analogRead(CIC_OK_PIN);
    if (v < mn) mn = v;
    if (v > mx) mx = v;
    const bool h = (v > 900);
    if (!first && h != prevHigh && tr < 255) tr++;
    prevHigh = h;

    const uint16_t k = analogRead(CIC_PROBE_PIN);
    if (k < kmn) kmn = k;
    if (k > kmx) kmx = k;
    const bool kh = (k > 900);
    if (!first && kh != kPrevHigh && ktr < 255) ktr++;
    kPrevHigh = kh;
    first = false;
  }
  obsKeyLo = (uint8_t)(kmn >> 2);
  obsKeyHi = (uint8_t)(kmx >> 2);
  obsKeyTransitions = ktr;
  obsLo = (uint8_t)(mn >> 2);
  obsHi = (uint8_t)(mx >> 2);
  obsTransitions = tr;
  obsEndedHigh = prevHigh;
}

void setup() {
  // /RESET はアクティブLow。先にポートビットを立ててから出力に切り替えると、
  // 一瞬もLowを出さずに済む。逆順だと数サイクルLowが出てカート内のチップが不用意に
  // リセットされる。なお電源投入からここに来るまでの間はINPUTのまま浮いている。
  digitalWrite(CART_RESET_PIN, HIGH);
  pinMode(CART_RESET_PIN, OUTPUT);

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
  // + パルス幅(2B,LE) + フラグ(1B)」の計9バイトを受け取る。タイミングはコンパイル時固定
  // ではなく、PC側がカートごとに「まず速い設定で試し、駄目なら段階的に上げる」ために毎回
  // 送ってくる。全バンク数はOLEDに「現在/全体」を表示するためだけに使う（読み出しには無関係）。
  // フラグのbit0が立っていればSRAM(セーブデータ)モード = /ROMSEL をアサートしない。
  // bit1が立っていれば読み出し中ずっとカートの /RESET をLowに保持する(コプロ実験用)。
  // 10バイト目はカートへ供給するクロックの分周値(OCR2A)。
  // bit3が立っていればCIC(F411A)を起動し、認証の成否を1バイト('A'/'N')返してから
  //   データを流す。SA-1カート用。クロックも自動で有効になる(CICはクロックが要るため)。
  // bit2が立っていればカートへクロックを供給する。既定は供給しない。
  //   Super FXでは供給すると逆効果だった。クロックが無い間GSUは眠っていてROMが見えるが、
  //   与えた途端に起きてバスを完全に奪い、読めるのはオープンバスだけになる。
  //   一方SA-1やCIC認証にはクロックが要るので、切り替えられるようにしてある。
  // 受信準備ができたことを 'R' で知らせてから待つ（先に送られると取りこぼすため）。
  oled.drawString(0, 2, "waiting bank..");
  Serial.write('R');
  uint8_t hdr[10];
  for (uint8_t i = 0; i < 10; i++) {
    while (Serial.available() == 0) { /* 待機 */ }
    hdr[i] = (uint8_t)Serial.read();
  }
  uint8_t target = hdr[0];
  uint8_t totalBanks = hdr[1];
  rdSettleUs   = (uint16_t)(hdr[2] | (hdr[3] << 8));
  addrSettleUs = (uint16_t)(hdr[4] | (hdr[5] << 8));
  pulseUs      = (uint16_t)(hdr[6] | (hdr[7] << 8));
  sramMode     = (hdr[8] & 0x01) != 0;
  const bool holdReset = (hdr[8] & 0x02) != 0;
  const bool cicMode = (hdr[8] & 0x08) != 0;
  // hdr[9] はクロックの分周値(OCR2A)。16MHz/(2*(1+n)) が出力周波数になる。
  //   n=7 -> 1MHz / n=3 -> 2MHz / n=2 -> 2.67MHz / n=1 -> 4MHz / n=0 -> 8MHz
  // CICの公称は3.072MHzだが16MHzからは整数分周で作れないので、近い値を掃引して探す。
  if ((hdr[8] & 0x04) || cicMode) startCartClock(hdr[9]);

  if (cicMode) {
    // クロックが安定してからリセットを解除したいので少し待つ
    delay(50);
    // 両方の極性を1秒ずつ観測して、生の結果をそのまま返す。判定はPC側で行う。
    // RSTはアクティブLowで確定済みなので、そちらだけ観測して10番と11番の両方を返す。
    cicObserve(true, 1000);
    Serial.write(obsLo); Serial.write(obsHi);
    Serial.write(obsTransitions); Serial.write(obsEndedHigh ? 1 : 0);
    Serial.write(obsKeyLo); Serial.write(obsKeyHi);
    Serial.write(obsKeyTransitions); Serial.write((uint8_t)0);
    // 認証が通らなくてもデータは流す。何が返ってくるかを見たいので。
  }

  // コプロを止めたままROMが覗けるか試すためのモード。バスを明け渡すかはチップ次第で、
  // 通るかどうかは実測するしかない。通常のROMカートでは /RESET は無関係。
  if (holdReset) digitalWrite(CART_RESET_PIN, LOW);

  char line[17];
  if (holdReset) {
    snprintf(line, sizeof(line), "RST Bank $%02X  ", (unsigned)target);
  } else if (sramMode) {
    snprintf(line, sizeof(line), "SRAM bank $%02X ", (unsigned)target);
  } else if (totalBanks > 0) {
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
