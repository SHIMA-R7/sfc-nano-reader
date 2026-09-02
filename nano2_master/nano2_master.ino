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

// 配線替え: このピンは元々カート57番(PHI2/SYSCK)に繋いでいたが、
// SA-1の解錠中にSYSCKを供給するとデータが壊れるという報告があったため、
// カート1番(MCK)へ繋ぎ直した。SA-1には自前の発振子が無く、MCKが無いと
// チップ全体がただの箱になる。CIC認証だけが通ってROMが0のままだった
// 症状は、これで説明がつく。
//
// D11でなければならない理由: ソフトでは出せずタイマーのハードウェア出力が要る。
// Nano-2で解放できるタイマー出力ピンはOC2A(=D11)だけ。
//
// 実機のMCKは21.477MHzだが、AVR(16MHz)のタイマーで単純トグルできる上限は
// 8MHz(Fclk/2)なので正確には出せない。OSCRは解錠時にあえて4MHzへ落として
// 供給し、成功後に21.477MHzへ戻している実績があるため、解錠時は4MHzで試す
// (startCartClockのocr=1が4MHz)。
const uint8_t CART_CLK_PIN = 11;

// カート33番(REFRESH)。SA-1に必要、Low固定で出力する。
// D13は基板上のLED+抵抗が乗るが、ここは静的なLow出力なので問題ない
// (OLEDデータ線や監視線のような双方向・高速・微弱信号ではない)。
const uint8_t REFRESH_PIN = 13;

// 起動時からカートへ出すクロックの分周値。-1 で「読み出し時だけ」。
// 16MHz/(2*(1+n)) が出力周波数。1=4MHz / 3=2MHz / 7=1MHz / 0=8MHz。
//
// **既定は -1（無効）。** Super FX ではカートへのクロックは有害で、与えた途端に
// GSUが起きてバスを完全に奪う（README「カートへのクロック供給は Super FX には逆効果」）。
// 全カートに無条件で流すわけにはいかない。
//
// なお、これを有効にしても読み出し要求時の `--clock` の代わりにはならない。
// スーパーマリオRPGのバンク$C0を安全段階で3回読むと、読み出しごとに明示指定した
// 場合は相違0、起動時クロックだけの場合は1〜2バイト化けた。Nano-2はシリアルを
// 開くたびリセットされるのでクロックも途切れる。sanniがSi5351(独立したIC)を
// 使って回しっぱなしにしているのは、この構造的な差を避けるためでもある。
// 起動時からカートへ出すクロックの分周値。-1 で「読み出し時だけ」。
// 16MHz/(2*(1+n)) が出力周波数。1=4MHz / 3=2MHz / 7=1MHz / 0=8MHz。
//
// **2026-08-28: 一度 1(4MHz常時) にして D11 -> カート56番 を試したが失敗した。**
// 「実データ率1.00」と出たので窓が消えたと判断したが、**判定が誤っていた**。
// 0x00 や 0x02 で埋まっていても「0xFFではない」ので1.00になる。
// 実際には KIRBY の文字列がROM内のどこにも存在せず、中身は空だった。
// Nano-3のD12が認証後に手放すのと同じ状態に戻す。
//
// **既定は -1（無効）。** Super FX ではカートへのクロックは有害で、与えた途端に
// GSUが起きてバスを完全に奪う（README「カートへのクロック供給は Super FX には逆効果」）。
const int8_t BOOT_CART_CLOCK_OCR = -1;

// 検証用。true にすると起動時クロックを約30Hzまで落とす（テスターで平均電圧を読むため）。
// 通常のダンプでは必ず false に戻すこと。
const bool BOOT_CART_CLOCK_SLOW = false;

// ■ sanniの起動シーケンスを再現する（2026-08-28）
//
// setup_Snes() を逐語で読み直したら、**周波数の解釈を間違えていた**。
// Si5351の set_freq は 0.01Hz 単位（後段の 2147727200 = 21.47727MHz から確定）。
// つまり起動時の設定は:
//
//     clockgen.set_freq(400000000ULL, SI5351_CLK0);  // マスター =  4.000MHz
//     clockgen.set_freq(100000000ULL, SI5351_CLK2);  // CIC     =  1.000MHz
//     clockgen.output_enable(SI5351_CLK1, 0);        // CPU     = 無効
//     delay(500);
//     PORTG &= ~(1 << 1);                            // CICリセット解除
//     delay(500);
//     getCartInfo_SNES();                            // ← ここでヘッダを読む
//     // **その後で** 21.477MHz / 3.072MHz へ上げる
//
// **ヘッダが読めるまでは低い周波数で動かしている。**
// こちらは最初から21.477MHzを流しっぱなしだった。順序が違う。
//
// 1MHzはNano-2のTimer2で誤差なく作れる: 16MHz/(2*(1+7)) = 1.000MHz (OCR2A=7)
//
// 0 にするとこの機能を使わない。
const uint16_t BOOT_CLOCK_BURST_MS = 0;

// CICクロック(カート56番)の分周値。7 = 1.000MHz（sanniの起動時と同じ）。
// -1 で出さない。
// -2 = D11を静的にLowで駆動 / -3 = 静的にHigh（配線が生きているかの確認用）
// **CICクロックは21.4MHzduinoが出すようになったので、-1(出さない)にする。**
// D11はカート56番から外れた。ここから出しても行き先が無い。
// 出力の衝突を避けるためにも無効のままにしておくこと。
const int8_t CIC_CLOCK_OCR = -1;
// Nano-3が電源投入300ms後にCICリセットを打つ。sanniはリセット解除後500ms待つので、
// それを覆う時間だけクロックを出し続けてから読み出しに入る。
const uint16_t CIC_SETTLE_MS = 900;

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
// slow=true では1024分周を掛ける。16MHz/1024/(2*(1+OCR2A))。OCR2A=255で約30Hz。
//
// なぜ低速モードが要るのか: 「発振しているか」をテスターで確かめるため。
// 50%デューティの方形波はDCモードで電源電圧の半分(2.5V)に見えるはずだが、
// 4MHzでは実測1.75Vだった。テスターのDC帯域は普通数kHz程度しかないので、
// この値は「発振していない」証拠にも「電圧不足」の証拠にもならない。
// 30Hzまで落とせばどんなテスターでも正確に平均でき、2.5Vちょうどが出れば
// 出力は健全（＝1.75Vは測定器側の帯域不足）と確定する。
void startCartClock(uint8_t ocr, bool slow) {
  pinMode(CART_CLK_PIN, OUTPUT);
  TCCR2A = _BV(COM2A0) | _BV(WGM21); // CTCモード、比較一致でOC2Aをトグル
  TCCR2B = slow ? (_BV(CS22) | _BV(CS21) | _BV(CS20))  // 1024分周
                : _BV(CS20);                            // 分周なし
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
// ■ 制御線のポート直叩き（2026-08-27 高速化）
// digitalWrite は1回**約4us**かかる。ピン番号からポートを引き、割り込みを止め、
// PWMを無効化する処理が毎回走るため。1バイトあたり6回呼んでいたので24us、
// tier1の実測77.7us/byteのうち3割がここだった。
//
// RD_PIN=2 / ROMSEL_PIN=3 / STROBE_PIN=4 はいずれも PORTD なので、
// ビット操作1命令(約0.06us)で置き換えられる。70倍速くなる。
//
// なお Nano-1 は**既に直叩き化済み**だった。addrSettleUs のコメントにある
// 「Nano-1側の16本分のdigitalWrite完了を待つマージン」は、直叩き化する前の
// 古い記述が残っていたもの。実コードは writeAddr() で3命令しか使っていない。
#define RD_HIGH()     (PORTD |= _BV(PD2))
#define RD_LOW()      (PORTD &= ~_BV(PD2))
#define ROMSEL_HIGH() (PORTD |= _BV(PD3))
#define ROMSEL_LOW()  (PORTD &= ~_BV(PD3))
#define STROBE_HIGH() (PORTD |= _BV(PD4))
#define STROBE_LOW()  (PORTD &= ~_BV(PD4))

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
  STROBE_HIGH();
  if (pulseUs) delayMicroseconds(pulseUs);
  STROBE_LOW();
  // Nano-1がPCINT割り込みでアドレスを更新し終えるのを待つ。
  // Nano-1側は既に直叩き(writeAddrは3命令)なので、必要なのは
  // 「割り込みが起動してISRが走り終わる」時間だけ。従来の20usは過剰の疑いがある。
  // ただし削りすぎるとアドレスがずれたまま読むので、正解の分かっている
  // カートで実測して決めること。
  if (addrSettleUs) delayMicroseconds(addrSettleUs);
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

// ■ sanni方式: /RD と /ROMSEL を Low に固定したまま読む（2026-08-28）
//
// sanni の setup_Snes() は起動時に一度だけ落として、以後**一切動かさない**:
//
//     PORTH &= ~((1 << 3) | (1 << 6));   // /CS と /RD を LOW
//
// そして readBank_SNES() は「アドレスを置く → 6NOP待つ → PINCを読む」だけ。
// **制御線をバイトごとに触らない。**
//
// こちらは1バイトごとに /ROMSEL と /RD を Low→High と往復させていた。
// 64KBなら65536回。SA-1は**バス調停チップ**なので、制御線が動くたびに
// 「SNES側が新しいバスサイクルを始めた」という信号になり、
// **そのたびに調停の機会を与えている**ことになる。
//
// 「バンクの途中で力尽きる」「数秒で閉じる」という実測は、
// この往復のどこかでSA-1にバスを奪われていると考えると筋が通る。
//
// HOLD_STROBES_LOW = 1 でsanni方式（固定）、0 で従来方式（往復）。
//
// **注意**: /RD をLow固定にするとROMが常時データを出し続ける。
// アドレス変更中も出力されるので、配線によってはバス競合や消費電流増が起きうる。
// sanniのMega構成では問題ないが、この配線で同じとは限らない。
#define HOLD_STROBES_LOW 0

// ══════════════════════════════════════════════════════════════════════
// カートへの書き込み（SA-1のBW-RAM窓を開けるためのレジスタ書き込み用）
//
// **既定では絶対に動かない。** ホストが flags bit6(0x40) を立てたときだけ通る。
// /WR を出すのは Nano-1 で、こちらは「長いストローブ」で指示する。
//
// ■ なぜ長いストローブなのか
// Nano-1 はPCと通信できず、知っているのは STROBE と RESET だけ。
// STROBE の立ち上がりでアドレスを+1したあと、まだHIGHのままなら書き込み指示、
// という約束にしてある。**アドレスを進めた直後なので目的の番地に書ける。**
// RESET幅で判定する案は、リセット直後でアドレスが必ず0になり使えなかった。
//
// ■ 手順
//   1. RESET（短く）           addr = 0
//   2. STROBE × (A-1) 回（短く） addr = A-1
//   3. データバスを出力にして値を置く
//   4. STROBE × 1 回（長く）    addr = A になり、その状態で /WR が出る
//   5. データバスを入力に戻す
//
// ■ 守っていること
//   ・データはストローブを上げる**前**に確定させる（/WRはISR内で即座に出るため）
//   ・書き終わったら必ず入力に戻す。カートと出力がぶつかる時間を最小にする
//   ・/ROMSEL は上げたまま（$2224 等は $8000 未満なのでROM領域ではない）
// ══════════════════════════════════════════════════════════════════════

// Nano-1 が「書き込み指示」と判定する閾値を確実に超える長さ。
// Nano-1 側は WR_COUNT_MIN 回のループで判定しており、実測前なので余裕を取る。
const uint16_t WRITE_STROBE_US = 200;

void setDataPinsOutput(uint8_t value) {
  // 先に値を確定させてから出力にする。出力にした瞬間に前の値が出ないように。
  for (uint8_t i = 0; i < 6; i++) digitalWrite(DATA_LOW_PINS[i], (value >> i) & 1);
  digitalWrite(D6_PIN, (value >> 6) & 1);
  digitalWrite(D7_PIN, (value >> 7) & 1);
  for (uint8_t i = 0; i < 6; i++) pinMode(DATA_LOW_PINS[i], OUTPUT);
  pinMode(D6_PIN, OUTPUT);
  pinMode(D7_PIN, OUTPUT);
  // pinMode後にもう一度書く。INPUT時のdigitalWriteはプルアップ設定なので。
  for (uint8_t i = 0; i < 6; i++) digitalWrite(DATA_LOW_PINS[i], (value >> i) & 1);
  digitalWrite(D6_PIN, (value >> 6) & 1);
  digitalWrite(D7_PIN, (value >> 7) & 1);
}

void pulseStrobeLong() {
  STROBE_HIGH();
  delayMicroseconds(WRITE_STROBE_US);   // Nano-1 がここで /WR を出す
  STROBE_LOW();
  delayMicroseconds(20);
}

// バンク0の address に data を1バイト書く。
// SA-1のレジスタ($2224/$2226/$2228)を叩くのが目的なので bank は0固定でよい。
void writeCartByte(uint16_t address, uint8_t data) {
  // **バンク(A16-A23)を0に戻すのを忘れてはいけない。**
  // ここを抜かしたまま実装し、起動読みで $E0 を読んだ直後の状態から書いていたため、
  // SA-1のレジスタがある $00:2224 ではなく $E0:2224 に書いていた。
  // 「窓が開かない」のではなく、窓を開ける場所に書いていなかった。
  resetNano3Bank();                       // A16-A23 = 0
  resetNano1Addr();                       // addr = 0
  for (uint16_t i = 0; i + 1 < address; i++) pulseStrobe();   // addr = address-1
  setDataPinsOutput(data);                // **ストローブより先に値を確定させる**
  if (addrSettleUs) delayMicroseconds(addrSettleUs);
  pulseStrobeLong();                      // addr = address になり /WR が出る
  setDataPinsInput();                     // **必ず入力へ戻す**
}

uint8_t readByte() {
#if HOLD_STROBES_LOW
  // 制御線はsetup()でLowにしたまま。ここでは触らない。
  if (rdSettleUs) delayMicroseconds(rdSettleUs);
  return readDataBus();
#else
  if (!sramMode) ROMSEL_LOW();
  RD_LOW();
  // delayMicroseconds(0) はArduinoの実装でループカウンタがアンダーフローし、
  // 極端に長く待つ既知の罠がある。0のときは呼ばない。
  if (rdSettleUs) delayMicroseconds(rdSettleUs);
  uint8_t v = readDataBus();
  RD_HIGH();
  if (!sramMode) ROMSEL_HIGH();
  return v;
#endif
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

// ── 簡易ロジックアナライザ ──────────────────────────────────────────
// analogRead()は1回に約100usかかる。CICの握手はそれよりずっと速く進むので、
// 「Low固定に見えた」のは単に速すぎて見えていなかっただけの可能性がある。
//
// ADCを毎回起動・停止するのをやめ、フリーランニング(連続変換)モードにすると
// 1変換13クロック。プリスケーラ8で16MHz/8=2MHz、つまり約6.5us間隔まで詰められる。
// 8bit精度(ADLARで上位バイトだけ読む)で十分——見たいのはHigh/Lowだけなので。
//
// 1サンプル1ビットに潰してRAMへ詰めるので、900バイトで7200サンプル＝約47ms分。
// CICの握手は内部計算を挟みながら進むので、開始部分を捉えるにはこれで足りる。
#define LA_BYTES 900
uint8_t laBuf[LA_BYTES];

// ch: 6=A6(CICの10番/リセット出力) 7=A7(CICのデータ線)
static void logicCapture(uint8_t ch, bool activeLow, uint8_t decim) {
  // ADC設定: 基準電圧AVCC、左詰め(上位8bitだけ読む)、指定チャンネル
  ADMUX = _BV(REFS0) | _BV(ADLAR) | (ch & 0x0F);
  ADCSRB = 0;                                   // フリーランニング
  // ADEN=有効 ADATE=自動トリガ ADPS=8分周(2MHz)。まだ開始しない。
  ADCSRA = _BV(ADEN) | _BV(ADATE) | _BV(ADPS1) | _BV(ADPS0);

  // 初回変換は25クロックかかる上、チャンネル切替直後の値は当てにならない。
  // 捨て変換を数回まわしてから本番に入る。ここを省いて同じ古い値を読み続け、
  // 「完全に静止」という誤った観測を得ていた。
  ADCSRA |= _BV(ADSC);
  for (uint8_t i = 0; i < 4; i++) {
    while (!(ADCSRA & _BV(ADIF))) { }
    ADCSRA |= _BV(ADIF);
  }

  // リセットを解除して、その瞬間から記録を始める。時間の基準点になる。
  //
  // 極性を引数で受けるのは、以前「アクティブLowで確定」と判断した根拠が、
  // 7-8番ピンが短絡していた時期の観測だったため。あの時はRSTピンにクロックが
  // 乗っていて、どちらの極性を指定してもチップは叩かれ続けていた。判定は無効。
  pinMode(CIC_RST_PIN, OUTPUT);
  digitalWrite(CIC_RST_PIN, activeLow ? LOW : HIGH);
  delay(20);
  digitalWrite(CIC_RST_PIN, activeLow ? HIGH : LOW);

  // 閾値で潰さず、8bitの生値をそのまま記録する。
  // 「完全に静止」という結果が測定系のバグなのか本当に信号が無いのかを、
  // 二値化した後のデータからは区別できない。生値なら中間電位もノイズも見える。
  // decim個に1個だけ記録して観測窓を伸ばす。握手の開始が遅い場合、
  // 6.5us刻みの5.8msでは窓の外に出てしまうため。
  for (uint16_t i = 0; i < LA_BYTES; i++) {
    for (uint8_t d = 0; d < decim; d++) {
      while (!(ADCSRA & _BV(ADIF))) { /* 変換完了待ち */ }
      laBuf[i] = ADCH;
      ADCSRA |= _BV(ADIF);
    }
  }
  ADCSRA = 0;   // ADCを止めて通常のanalogRead()に戻せるようにする
}

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

#if HOLD_STROBES_LOW
  // sanni方式。起動時にLowへ落として以後動かさない。
  pinMode(RD_PIN, OUTPUT); digitalWrite(RD_PIN, LOW);
  pinMode(ROMSEL_PIN, OUTPUT); digitalWrite(ROMSEL_PIN, LOW);
#else
  pinMode(RD_PIN, OUTPUT); digitalWrite(RD_PIN, HIGH);
  pinMode(ROMSEL_PIN, OUTPUT); digitalWrite(ROMSEL_PIN, HIGH);
#endif
  pinMode(STROBE_PIN, OUTPUT); digitalWrite(STROBE_PIN, LOW);
  pinMode(BANK_RESET_PIN, OUTPUT); digitalWrite(BANK_RESET_PIN, LOW);
  pinMode(BANK_STROBE_PIN, OUTPUT); digitalWrite(BANK_STROBE_PIN, LOW);
  pinMode(ADDR_RESET_PIN, OUTPUT); digitalWrite(ADDR_RESET_PIN, LOW);
  pinMode(REFRESH_PIN, OUTPUT); digitalWrite(REFRESH_PIN, LOW);  // SA-1に必要
  setDataPinsInput();

  // 起動直後からカートへクロックを出し続ける。
  //
  // 従来は「読み出しの要求が来たとき」にだけ startCartClock を呼んでいた。しかし
  // Nano-3は電源投入の300ms後にCIC握手を実行するので、**握手の最中もリセット解除の
  // 時点も、カート1番には一度もクロックが来ていなかった。** SA-1のようなバス調停
  // チップが起動時点の状態をラッチするなら、後からクロックを与えても手遅れになる。
  //
  // 実測でも、読み出し中のクロック供給の有無は結果を1ビットも変えなかった
  // (I-RAM窓0x34 / BW-RAM窓0xFF / ROM窓0x00 で完全に同一)。
  // BOOT_CART_CLOCK_OCR を -1 にすれば従来の挙動に戻る。
  if (BOOT_CART_CLOCK_OCR >= 0) {
    startCartClock(BOOT_CART_CLOCK_SLOW ? 255 : (uint8_t)BOOT_CART_CLOCK_OCR,
                   BOOT_CART_CLOCK_SLOW);
  } else if (CIC_CLOCK_OCR == -2 || CIC_CLOCK_OCR == -3) {
    // 配線の生死を確かめるための静的駆動。クロックではなく直流を出す。
    // これで応答が変われば D11 -> カート56番 は繋がっている。
    DDRB |= _BV(PB3);
    if (CIC_CLOCK_OCR == -3) PORTB |= _BV(PB3); else PORTB &= (uint8_t)~_BV(PB3);
  } else if (CIC_CLOCK_OCR >= 0) {
    // sanniの起動シーケンス相当。CICクロックを1MHzで出し、
    // Nano-3のリセット(300ms後)とその後の落ち着きを待つ。
    // **クロックは止めない。** sanniは CLK2 を立てたきり切らない。
    startCartClock((uint8_t)CIC_CLOCK_OCR, false);
    delay(CIC_SETTLE_MS);
  } else if (BOOT_CLOCK_BURST_MS > 0) {
    // D12の再現。一定時間クロックを出してから手放す。
    startCartClock(1, false);          // 4.000MHz
    delay(BOOT_CLOCK_BURST_MS);
    // タイマー出力を切り離し、ピンを入力に戻す（＝浮かせる）。
    // D12が DDRB &= ~CLK_BIT でやっていたのと同じ状態にする。
    TCCR2A = 0;
    TCCR2B = 0;
    PORTB &= (uint8_t)~_BV(PB3);       // D11 = PB3。プルアップを付けない
    DDRB  &= (uint8_t)~_BV(PB3);       // 入力へ戻す
  }

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
  // bit4が立っていればロジックアナライザモード。CICのリセットを解除した瞬間から
  //   約6.5us間隔でピンを記録し、900バイト(7200サンプル)を返す。
  //   hdr[1]の下位1bitで対象を選ぶ: 0=A6(10番/リセット出力) 1=A7(データ線)
  // bit3が立っていればCIC(F411A)を起動し、認証の成否を1バイト('A'/'N')返してから
  //   データを流す。SA-1カート用。クロックも自動で有効になる(CICはクロックが要るため)。
  // bit2が立っていればカートへクロックを供給する。既定は供給しない。
  //   Super FXでは供給すると逆効果だった。クロックが無い間GSUは眠っていてROMが見えるが、
  //   与えた途端に起きてバスを完全に奪い、読めるのはオープンバスだけになる。
  //   一方SA-1やCIC認証にはクロックが要るので、切り替えられるようにしてある。
  // 受信準備ができたことを 'R' で知らせてから待つ（先に送られると取りこぼすため）。
  oled.drawString(0, 2, "waiting bank..");
  Serial.write('R');
  // 11バイト目は「続けて読むバンク数」(連続バンクモード)。
  // 旧ホストは10バイトしか送らないので、11バイト目は短いタイムアウト付きで待ち、
  // 来なければ0(=1バンクで停止、従来動作)とみなす。こうしないと旧ホストが固まる。
  uint8_t hdr[11] = {0};
  for (uint8_t i = 0; i < 10; i++) {
    while (Serial.available() == 0) { /* 待機 */ }
    hdr[i] = (uint8_t)Serial.read();
  }
  {
    const uint32_t deadline = millis() + 50;
    while (Serial.available() == 0 && millis() < deadline) { /* 待機 */ }
    if (Serial.available() > 0) hdr[10] = (uint8_t)Serial.read();
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
  // ── 書き込みモード（bit6）。**既定では絶対に通らない経路。**
  // SA-1のBW-RAM窓を開けるためのレジスタ書き込みに使う。
  // ヘッダの解釈が読み出しと変わる:
  //     hdr[0..1] : 書き込み先アドレス（下位, 上位）
  //     hdr[2]    : 書き込む値
  //     hdr[3]    : 繰り返し回数（1でよい。取りこぼし対策で増やせる）
  // 応答は 'W' 1バイト。読み出しのようにデータは返さない。
  if (hdr[8] & 0x40) {
    const uint16_t waddr = (uint16_t)(hdr[0] | (hdr[1] << 8));
    const uint8_t  wdata = hdr[2];
    const uint8_t  wrep  = hdr[3] ? hdr[3] : 1;
    sramMode = true;              // /ROMSEL を上げたままにする（$8000未満のため）
    ROMSEL_HIGH();
    for (uint8_t i = 0; i < wrep; i++) writeCartByte(waddr, wdata);
    setDataPinsInput();           // 念のためもう一度
    Serial.write('W');
    Serial.flush();
    return;                       // 読み出し経路には入らない
  }

  if ((hdr[8] & 0x04) || cicMode) startCartClock(hdr[9], false);

  const bool laMode = (hdr[8] & 0x10) != 0;
  if (laMode) {
    delay(50);                       // クロックが安定するのを待つ
    // hdr[1] bit0=観測対象(0:A6 1:A7)  bit1=RST極性(0:アクティブLow 1:アクティブHigh)
    logicCapture((hdr[1] & 1) ? 7 : 6, (hdr[1] & 2) == 0,
                 hdr[1] >> 2 ? hdr[1] >> 2 : 1);  // bit2以上=間引き率
    Serial.write(laBuf, LA_BYTES);
    while (1) { /* 記録を返したら停止 */ }
  }

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

  // SA-1向けの「慣らし」。OSCR(sanni/cartreader)が実際に行っている手順で、
  // MCK供給・CIC認証が済んだ直後、バンク$C0で1024回ダミーアクセスしてから
  // 本読みに入る。これが無いと最初の数バイトで安定しないことがあるとされる。
  const bool primeMode = (hdr[8] & 0x20) != 0;
  if (primeMode) {
    resetNano1Addr();
    resetNano3Bank();
    for (uint16_t b = 0; b < 0xC0; b++) pulseBankStrobe();
    for (uint16_t a = 0; a < 1024; a++) {
      readByte();
      pulseStrobe();
    }
  }

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

  // 連続バンクモード: hdr[10] に「このセッションで続けて読むバンク数」が入る。
  // 0または1なら従来どおり1バンクで停止する。
  //
  // ■ なぜ必要か
  // 従来はPCがシリアルを開くたびDTRでNano-2がリセットされ、1バンクごとに
  // 起動処理をやり直していた。バンクあたり約2秒の固定費で、64バンクなら2分強を
  // 捨てていた。通常のROMなら気にならないが、**SA-1は時間とともに提示する内容を
  // 変えてしまう**（実測: 同じバンクを54秒間読み続けると2つの状態を行き来する）。
  // 読み切るまでの時間そのものが正しさに直結するので、ここを削る。
  //
  // sanniは1バイトあたり375ns(アドレス設定+6NOP)で読む。このリグは約7000ns。
  // 1バイトの差は埋められないが、バンクあたりの固定費は消せる。
  const uint8_t runBanks = (hdr[10] == 0) ? 1 : hdr[10];

  char prog[17];
  for (uint8_t bank = 0; bank < runBanks; bank++) {
    if (bank > 0) {
      // 次のバンクへ。アドレスカウンタだけ0に戻し、バンクは+1する。
      // ここでNano-2自身はリセットしない（それが節約の本体）。
      resetNano1Addr();
      pulseBankStrobe();
    }
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
  }
  snprintf(prog, sizeof(prog), "%5lu/%5lu", (unsigned long)BANK_SIZE, (unsigned long)BANK_SIZE);
  oled.drawString(0, 4, prog);

  oled.drawString(0, 2, "DONE          ");

  while (1) {
    // 完了。ここで停止し続ける
  }
}

void loop() {}
