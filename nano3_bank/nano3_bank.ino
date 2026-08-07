// Nano-3: cart A16-A23（バンク）カウンタ ＋ OLED進捗表示
// Nano-1のアドレスカウンタと同じ「STROBEで+1、RESETで0」方式。
// 旧DATA/CLK/LATCHのシフトレジスタ方式は、2枚のNanoが非同期クロックで動くため
// タイミングのレースでビットを取りこぼし不安定だったので廃止した。
// 配線は変更していない。旧BANK_DATA線(D10)をRESET、旧BANK_CLK線(D11)をSTROBEとして使う。
// 旧BANK_LATCH線(D12)は未使用。
//
// Nano-1と同じ理由でピン変化割り込みを使う（ポーリングだとパルスを取りこぼす）。
//
// ■ OLEDをNano-2からこちらへ移した理由
// 元はNano-2が読み出しループの中で8192バイトごとにI2Cをビットバンギングしていた。
// 読み出しの真っ最中に数msかけてピンを叩き、表示モジュールが電流を食う設計で、
// 読み取り品質への影響が疑われた。またNano-2のD11をカートのクロック出力に明け渡した際、
// OLEDのデータ線をD13へ逃がしたところ基板上のLEDと抵抗が負荷になって表示できなくなり、
// Nano-2には他に空きピンが無かった。
//
// Nano-3は本来ほぼ遊んでいる上に、**バンク番号をすでに知っている**（ストローブを数えた
// 値がそのままバンク番号）。バイト位置もNano-2→Nano-1のSTROBE線を分岐して数えれば分かる。
// 表示が多少もたついても、あるいは取りこぼしても、**ダンプの正しさには一切影響しない**。
//
// ピン割り当てとポートの対応:
//   A16-A21 -> D2-D7  = PORTD bit2-7
//   A22-A23 -> D8-D9  = PORTB bit0-1
//   RESET   -> D10    = PINB bit2 (PCINT2)
//   STROBE  -> D11    = PINB bit3 (PCINT3)
//   OLED SCL-> A0                          (ソフトI2C。D13はLEDが負荷になるので使わない)
//   OLED SDA-> A1
//   BYTE STB-> A2     = PINC bit2 (PCINT10) Nano-2 D4から分岐した1バイトごとのパルス

#include <U8x8lib.h>

const uint8_t BANK_PINS[8] = {2, 3, 4, 5, 6, 7, 8, 9}; // A16..A23
const uint8_t RESET_PIN = 10;
const uint8_t STROBE_PIN = 11;
const uint8_t BYTE_STROBE_PIN = A2;

#define RESET_BIT  _BV(PB2)
#define STROBE_BIT _BV(PB3)
#define BYTE_BIT   _BV(PC2)

U8X8_SH1106_128X64_NONAME_SW_I2C oled(/*clock=*/ A0, /*data=*/ A1, /*reset=*/ U8X8_PIN_NONE);

volatile uint8_t bank = 0;
volatile uint16_t byteCount = 0;

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
    byteCount = 0;   // 1バンク読み出しの開始。バイト位置も0から
    writeBank(0);
  } else if (rose & STROBE_BIT) {
    writeBank(++bank);
  }
}

// PCINT1 は PORTC の変化。A2(バイトストローブ)だけを有効にしてある。
// 1バイトあたり47us程度なので約21kHz。表示用の数え上げなので、
// OLED更新中に取りこぼしても表示がわずかにずれるだけで実害はない。
ISR(PCINT1_vect) {
  static uint8_t last = 0;
  const uint8_t now = PINC & BYTE_BIT;
  if (now && !last) byteCount++;
  last = now;
}

void setup() {
  for (uint8_t i = 0; i < 8; i++) pinMode(BANK_PINS[i], OUTPUT);
  pinMode(RESET_PIN, INPUT);
  pinMode(STROBE_PIN, INPUT);
  pinMode(BYTE_STROBE_PIN, INPUT);
  writeBank(0);

  // 以前はここで TIMSK0 = 0 として millis() を止めていた。ポーリングで
  // パルスを追っていた頃、タイマ割り込みが邪魔だったため。割り込み方式に
  // 変えた時点でその理由は消えており、U8x8が delay() を使うので有効に戻す。

  oled.begin();
  oled.setFont(u8x8_font_chroma48medium8_r);
  oled.clear();
  oled.drawString(0, 0, "SFC DUMPER");
  oled.drawString(0, 2, "waiting...");

  PCICR |= _BV(PCIE0) | _BV(PCIE1);
  PCMSK0 = STROBE_BIT | RESET_BIT;
  PCMSK1 = _BV(PCINT10);   // A2のみ。A0/A1はI2C出力なので絶対に含めない
  sei();
}

void loop() {
  // 割り込みで進む値を、人が読める速さで描くだけ。
  // I2Cは数ms掛かるので、毎回描くと割り込みを塞ぐ時間が増える。
  static uint32_t lastDraw = 0;
  static uint8_t shownBank = 255;
  if (millis() - lastDraw < 120) return;
  lastDraw = millis();

  uint8_t b;
  uint16_t n;
  noInterrupts();          // 16bitの読み出しは分割されると壊れるので止めて読む
  b = bank;
  n = byteCount;
  interrupts();

  char line[17];
  if (b != shownBank) {
    shownBank = b;
    snprintf(line, sizeof(line), "Bank $%02X       ", (unsigned)b);
    oled.drawString(0, 2, line);
  }
  snprintf(line, sizeof(line), "%5u/65536    ", (unsigned)n);
  oled.drawString(0, 4, line);
}
