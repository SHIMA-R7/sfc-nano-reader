# -*- coding: utf-8 -*-
"""論文体裁PDFの共通部品: スタイル・表・囲み・図版。

方針:
  ・数値はすべて README / docs/HISTORY.md / SFC-CIC/NOTES.md / 実ファイルの実測から取る
  ・図は reportlab.graphics で自前に描く（外部画像に依存しない）
"""
import math

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.fonts import addMapping
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.graphics.shapes import (Drawing, Rect, String, Line, PolyLine,
                                       Circle, Group)
from reportlab.platypus import KeepTogether, Paragraph, Table, TableStyle

pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))
GOTHIC = "HeiseiKakuGo-W5"
MINCHO = "HeiseiMin-W3"
# CIDフォントに太字フェイスは無い。明朝の太字要求をゴシックへ回して強調を可視化する。
addMapping(MINCHO, 0, 0, MINCHO)
addMapping(MINCHO, 1, 0, GOTHIC)
addMapping(GOTHIC, 0, 0, GOTHIC)
addMapping(GOTHIC, 1, 0, GOTHIC)

INK = colors.HexColor("#161616")
GRAY = colors.HexColor("#5c5c5c")
FAINT = colors.HexColor("#8d8d8d")
RULE = colors.HexColor("#c9c9c9")
RED = colors.HexColor("#9a2b1e")
BLUE = colors.HexColor("#1f4e79")
TEAL = colors.HexColor("#1f6f6f")
AMBER = colors.HexColor("#a8710f")
GREEN = colors.HexColor("#2e7d32")
PURPLE = colors.HexColor("#5b3d80")
BG = colors.HexColor("#f5f2ec")
BGBLUE = colors.HexColor("#eaf0f6")
BGRED = colors.HexColor("#f8ecea")
BGGRN = colors.HexColor("#eaf3ea")
BGAMB = colors.HexColor("#faf1e0")

W = A4[0] - 38 * mm


def st(name, **kw):
    base = dict(fontName=MINCHO, fontSize=9.2, leading=15.4, textColor=INK)
    base.update(kw)
    return ParagraphStyle(name, **base)


S = {
    "title": st("title", fontName=GOTHIC, fontSize=18, leading=26,
                alignment=TA_CENTER, spaceAfter=6),
    "subtitle": st("subtitle", fontSize=10.5, leading=17.5,
                   alignment=TA_CENTER, textColor=GRAY, spaceAfter=4),
    "byline": st("byline", fontSize=8.8, leading=15, alignment=TA_CENTER,
                 textColor=FAINT, spaceAfter=14),
    "h1": st("h1", fontName=GOTHIC, fontSize=13.5, leading=20, textColor=RED,
             spaceBefore=16, spaceAfter=6, keepWithNext=1),
    "h2": st("h2", fontName=GOTHIC, fontSize=10.6, leading=17, textColor=BLUE,
             spaceBefore=12, spaceAfter=4, keepWithNext=1),
    "h3": st("h3", fontName=GOTHIC, fontSize=9.5, leading=15, textColor=INK,
             spaceBefore=9, spaceAfter=3, keepWithNext=1),
    "body": st("body", alignment=TA_JUSTIFY, spaceAfter=6.5),
    "abst": st("abst", fontSize=9, leading=15.5, alignment=TA_JUSTIFY,
               leftIndent=6, rightIndent=6, spaceAfter=6),
    "note": st("note", fontSize=8.6, leading=14, textColor=GRAY,
               leftIndent=10, rightIndent=6, spaceBefore=2, spaceAfter=7),
    "code": st("code", fontName=GOTHIC, fontSize=8.2, leading=13.4,
               leftIndent=9, textColor=colors.HexColor("#2c2c2c"),
               spaceBefore=3, spaceAfter=7),
    "cap": st("cap", fontSize=8.3, leading=13, textColor=GRAY,
              alignment=TA_CENTER, spaceBefore=3, spaceAfter=11),
    "tcap": st("tcap", fontName=GOTHIC, fontSize=8.6, leading=13.5,
               textColor=INK, spaceBefore=8, spaceAfter=3, keepWithNext=1),
    "cell": st("cell", fontSize=8.2, leading=12.6, spaceAfter=0),
    "cellh": st("cellh", fontName=GOTHIC, fontSize=8.2, leading=12.6,
                textColor=colors.white, spaceAfter=0),
    "cellc": st("cellc", fontSize=8.2, leading=12.6, alignment=TA_CENTER,
                spaceAfter=0),
    "ref": st("ref", fontSize=8.3, leading=13.4, leftIndent=13,
              firstLineIndent=-13, spaceAfter=3.5),
}


def P(t, s="body"):
    return Paragraph(t, S[s])


def table(rows, widths, hdr_color=BLUE, align=None, zebra=True):
    align = align or set()
    data = []
    for i, row in enumerate(rows):
        cells = []
        for j, c in enumerate(row):
            if i == 0:
                cells.append(Paragraph(c, S["cellh"]))
            else:
                cells.append(Paragraph(c, S["cellc" if j in align else "cell"]))
        data.append(cells)
    t = Table(data, colWidths=widths, hAlign="LEFT", repeatRows=1)
    style = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3.6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3.6),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("BACKGROUND", (0, 0), (-1, 0), hdr_color),
        ("LINEBELOW", (0, 1), (-1, -2), 0.35, RULE),
        ("LINEBELOW", (0, -1), (-1, -1), 0.8, hdr_color),
    ]
    if zebra:
        for i in range(2, len(rows), 2):
            style.append(("BACKGROUND", (0, i), (-1, i), BG))
    t.setStyle(TableStyle(style))
    # 短い表が1行だけ次ページへ落ちると読めなくなる。まとめて送る。
    return KeepTogether(t) if len(rows) <= 9 else t


def box(paras, color=BGBLUE, edge=BLUE):
    inner = Table([[p] for p in paras], colWidths=[W - 22], hAlign="LEFT")
    inner.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
    ]))
    outer = Table([[inner]], colWidths=[W], hAlign="LEFT")
    outer.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), color),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, edge),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ("LEFTPADDING", (0, 0), (-1, -1), 9),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
    ]))
    return outer


# ───────────────────────────────────────── 図版の下請け
def _t(g, x, y, s, size=7.4, col=INK, anchor="middle", font=GOTHIC):
    g.add(String(x, y, s, fontName=font, fontSize=size, fillColor=col,
                 textAnchor=anchor))


def _box(g, x, y, w, h, label, sub=None, fill=BGBLUE, edge=BLUE, tcol=INK):
    g.add(Rect(x, y, w, h, fillColor=fill, strokeColor=edge, strokeWidth=0.9))
    if sub:
        _t(g, x + w / 2, y + h / 2 + 2, label, 8, tcol)
        _t(g, x + w / 2, y + h / 2 - 8, sub, 6.6, GRAY)
    else:
        _t(g, x + w / 2, y + h / 2 - 3, label, 8, tcol)


def _arrow(g, x1, y1, x2, y2, col=INK, w=0.9, head=4.4):
    g.add(Line(x1, y1, x2, y2, strokeColor=col, strokeWidth=w))
    a = math.atan2(y2 - y1, x2 - x1)
    for s in (1, -1):
        g.add(Line(x2, y2, x2 - head * math.cos(a - s * 0.42),
                   y2 - head * math.sin(a - s * 0.42),
                   strokeColor=col, strokeWidth=w))


# ───────────────────────────────────────── 図
def fig_block():
    d = Drawing(W, 200)
    g = Group()
    _box(g, 170, 140, 170, 46, "SFCカートリッジ", "62ピン カードエッジ",
         BGRED, RED)
    _box(g, 8, 64, 118, 46, "Nano-1", "A0-A15 生成", BGGRN, GREEN)
    _box(g, 190, 60, 130, 54, "Nano-2（マスタ）", "/RD /ROMSEL・D0-D7",
         BGBLUE, BLUE)
    _box(g, 384, 64, 118, 46, "Nano-3", "A16-A23 生成", BGGRN, GREEN)
    _box(g, 200, 4, 110, 30, "PC", None, colors.white, INK)
    _arrow(g, 67, 110, 67, 163, GREEN)
    _arrow(g, 67, 163, 168, 163, GREEN)
    _t(g, 105, 167, "アドレス A0-A15", 6.8, GREEN)
    _arrow(g, 443, 110, 443, 163, GREEN)
    _arrow(g, 443, 163, 342, 163, GREEN)
    _t(g, 405, 167, "バンク A16-A23", 6.8, GREEN)
    _arrow(g, 243, 114, 243, 138, BLUE, 1.3)
    _arrow(g, 268, 138, 268, 114, RED, 1.3)
    _t(g, 238, 124, "/RD /ROMSEL", 6.6, BLUE, "end")
    _t(g, 274, 124, "D0-D7", 6.6, RED, "start")
    _arrow(g, 188, 86, 130, 86, BLUE)
    _t(g, 159, 90, "STROBE", 6.6, BLUE)
    _arrow(g, 322, 86, 380, 86, BLUE)
    _t(g, 351, 90, "STROBE", 6.6, BLUE)
    _arrow(g, 255, 58, 255, 36, INK)
    _t(g, 318, 46, "USB 1 Mbps", 6.8, GRAY, "start")
    _t(g, 67, 54, "PCINTでエッジをラッチ", 6.4, GRAY)
    _t(g, 443, 54, "PCINTでエッジをラッチ", 6.4, GRAY)
    d.add(g)
    return d


def fig_lorom():
    d = Drawing(W, 132)
    g = Group()
    x0, w = 66, 152
    for lab, y in (("バンク $01", 66), ("バンク $00", 12)):
        g.add(Rect(x0, y, w, 42, fillColor=colors.HexColor("#dfe8f1"),
                   strokeColor=BLUE, strokeWidth=0.8))
        g.add(Rect(x0, y + 21, w, 21, fillColor=colors.HexColor("#bed3e6"),
                   strokeColor=BLUE, strokeWidth=0.8))
        _t(g, x0 - 6, y + 18, lab, 7.4, INK, "end")
        _t(g, x0 + w / 2, y + 28, "$8000-$FFFF  ROM 32KB", 6.9, BLUE)
        _t(g, x0 + w / 2, y + 7, "$0000-$7FFF  ミラー", 6.9, GRAY)
    _t(g, x0 + w / 2, 118, "実際のLoROM ─ ミラーは「バンクの中」", 8, GREEN)
    x1 = 316
    for y, lab, sub in ((66, "後半32バンク", "＝前半のミラー"),
                        (12, "前半32バンク", "＝実データ")):
        g.add(Rect(x1, y, w, 42, fillColor=colors.HexColor("#f0e2e0"),
                   strokeColor=RED, strokeWidth=0.8))
        _t(g, x1 + w / 2, y + 26, lab, 7.4, RED)
        _t(g, x1 + w / 2, y + 12, sub, 6.9, GRAY)
    _t(g, x1 + w / 2, 118, "よくある誤解（バンク単位のミラー）", 8, RED)
    g.add(Line(x1 + 8, 18, x1 + w - 8, 102, strokeColor=RED, strokeWidth=1.8))
    g.add(Line(x1 + 8, 102, x1 + w - 8, 18, strokeColor=RED, strokeWidth=1.8))
    d.add(g)
    return d


def fig_strobe():
    d = Drawing(W, 172)
    g = Group()
    x0, pitch, hi = 100, 40, 13
    ys = [130, 88, 32]
    _t(g, x0 - 8, ys[0] + 3, "STROBE", 7.4, INK, "end")
    pts = []
    for i in range(8):
        x = x0 + i * pitch
        pts += [x, ys[0], x, ys[0] + hi, x + 11, ys[0] + hi, x + 11, ys[0]]
    pts += [x0 + 8 * pitch, ys[0]]
    g.add(PolyLine(pts, strokeColor=INK, strokeWidth=1.1))
    for i in range(8):
        _t(g, x0 + i * pitch + 5, ys[0] + hi + 5, str(i + 1), 6.2, GRAY)
    _t(g, x0 - 8, ys[1] + 3, "ポーリング", 7.4, RED, "end")
    for i in range(8):
        x = x0 + i * pitch + 5
        ok = (i % 2 == 0)
        g.add(Circle(x, ys[1] + 4, 5.0, fillColor=BGGRN if ok else BGRED,
                     strokeColor=GREEN if ok else RED, strokeWidth=1))
        _t(g, x, ys[1] + 1.4, "○" if ok else "×", 6.6,
           GREEN if ok else RED)
    _t(g, x0 + 8 * pitch + 12, ys[1] + 7, "1回おきに落ちる", 7.2, RED, "start")
    _t(g, x0 + 8 * pitch + 12, ys[1] - 4, "→ 同じアドレスを2回読む", 6.6,
       GRAY, "start")
    _t(g, x0 - 8, ys[2] + 3, "PCINT", 7.4, GREEN, "end")
    for i in range(8):
        x = x0 + i * pitch + 5
        g.add(Circle(x, ys[2] + 4, 5.0, fillColor=BGGRN, strokeColor=GREEN,
                     strokeWidth=1))
        _t(g, x, ys[2] + 1.4, "○", 6.6, GREEN)
    _t(g, x0 + 8 * pitch + 12, ys[2] + 7, "ハードウェアがラッチ", 7.2, GREEN,
       "start")
    _t(g, x0 + 8 * pitch + 12, ys[2] - 4, "→ 穴そのものが消える", 6.6, GRAY,
       "start")
    g.add(Line(56, 62, W - 12, 62, strokeColor=RULE, strokeWidth=0.6))
    d.add(g)
    return d


def fig_tier_chart():
    """図: タイミング段階に対する相違バイト数（対数目盛・自前描画）"""
    d = Drawing(W, 208)
    g = Group()
    ox, oy, pw, ph = 74, 40, W - 190, 138
    tiers = ["5us", "20us", "50us", "100us", "300us"]
    series = [
        ("かまいたちの夜 $00（タイミング不足）", [3238, 1028, 438, 110, 1], BLUE),
        ("かまいたちの夜 $28（同上）", [1502, 239, 51, 7, 0.5], TEAL),
        ("スターフォックス（電源不足）", [89, 338, 5259, 1969, 2524], RED),
    ]
    def ly(v):
        v = max(v, 0.5)
        return oy + ph * (math.log10(v) + 0.301) / (math.log10(10000) + 0.301)
    for e in (0, 1, 2, 3, 4):
        yy = ly(10 ** e)
        g.add(Line(ox, yy, ox + pw, yy, strokeColor=RULE, strokeWidth=0.4))
        _t(g, ox - 6, yy - 2.5, ["1", "10", "100", "1,000", "10,000"][e], 6.6,
           GRAY, "end", MINCHO)
    g.add(Line(ox, oy, ox, oy + ph, strokeColor=INK, strokeWidth=0.8))
    g.add(Line(ox, oy, ox + pw, oy, strokeColor=INK, strokeWidth=0.8))
    for i, t in enumerate(tiers):
        x = ox + pw * (i + 0.5) / 5
        _t(g, x, oy - 12, t, 7, INK)
    _t(g, ox - 6, oy + ph + 8, "相違バイト数（対数）", 7, GRAY, "end")
    _t(g, ox + pw / 2, oy - 25, "読み出しタイミング段階（遅くなる →）", 7.2, GRAY)
    for name, vals, col in series:
        pts = []
        for i, v in enumerate(vals):
            x = ox + pw * (i + 0.5) / 5
            pts += [x, ly(v)]
            g.add(Circle(x, ly(v), 2.6, fillColor=col, strokeColor=col))
        g.add(PolyLine(pts, strokeColor=col, strokeWidth=1.6))
    yl = oy + ph + 22
    for i, (name, _, col) in enumerate(series):
        xx = ox + (i % 2) * 250
        yy = yl + (0 if i < 2 else -12)
        g.add(Rect(xx, yy - 1, 16, 3.4, fillColor=col, strokeColor=col))
        _t(g, xx + 21, yy - 3, name, 7, INK, "start")
    d.add(g)
    return d


def fig_speed():
    """図: 1バイトあたりの時間の内訳（積み上げ）"""
    d = Drawing(W, 176)
    g = Group()
    ox, oy, pw = 96, 46, W - 190
    scale = pw / 90.0
    rows = [
        ("331us（初期）", [(0, 331, colors.HexColor("#d6c9c6"), "digitalWrite と過剰な待ち")]),
        ("80us（20/20/5）", [(0, 34.1, BLUE, "固定費 34us"),
                            (34.1, 45, AMBER, "待ち 45us")]),
        ("47us（5/5/3 採用）", [(0, 34.1, BLUE, "固定費"), (34.1, 13, AMBER, "待ち13us")]),
        ("36us（待ち0の床）", [(0, 10, TEAL, "シリアル 10us"),
                            (10, 24.1, PURPLE, "実装の間接費 26us")]),
    ]
    for i, (lab, segs) in enumerate(rows):
        y = oy + (3 - i) * 26
        _t(g, ox - 8, y + 5, lab, 7.2, INK, "end")
        for x0, wv, col, _n in segs:
            w = min(wv, 90 - x0) * scale
            if w <= 0:
                continue
            g.add(Rect(ox + x0 * scale, y, w, 14, fillColor=col,
                       strokeColor=colors.white, strokeWidth=0.6))
        if i == 0:
            _t(g, ox + 90 * scale + 6, y + 5, "（89us で図の外へ続く）", 6.8,
               GRAY, "start")
    for v in (0, 20, 40, 60, 80):
        x = ox + v * scale
        g.add(Line(x, oy - 4, x, oy - 8, strokeColor=GRAY, strokeWidth=0.5))
        _t(g, x, oy - 18, str(v), 6.6, GRAY, "middle", MINCHO)
    _t(g, ox + pw / 2, oy - 30, "1バイトあたりの所要時間 [us]", 7.2, GRAY)
    leg = [("固定費（回帰の切片 34.1us）", BLUE), ("待ち時間（傾き1.011）", AMBER),
           ("うちシリアル1Mbps 10us", TEAL), ("うち実装の間接費 26us", PURPLE)]
    for i, (n, c) in enumerate(leg):
        xx = ox + (i % 2) * 230
        yy = oy + 122 - (i // 2) * 13
        g.add(Rect(xx, yy, 14, 7, fillColor=c, strokeColor=c))
        _t(g, xx + 19, yy + 0.5, n, 6.8, INK, "start")
    d.add(g)
    return d


def fig_topology():
    """図: クロックを与えるかどうかで、SA-1の見え方が反転する"""
    d = Drawing(W, 214)
    g = Group()
    _t(g, W / 2, 202, "SA-1は「直列だから読めない」のではなかった。眠らせれば素通しになる",
       8.2, INK)
    y = 150
    _box(g, 26, y, 74, 30, "ROM", None, BGGRN, GREEN)
    _box(g, 140, y, 82, 30, "SA-1", "眠っている", BG, GRAY)
    _box(g, 262, y, 88, 30, "カートエッジ", None, colors.white, INK)
    g.add(Line(100, y + 15, 140, y + 15, strokeColor=GREEN, strokeWidth=1.9))
    g.add(Line(222, y + 15, 262, y + 15, strokeColor=GREEN, strokeWidth=1.9))
    _t(g, 20, y + 11, "クロック無し", 8, GREEN, "end")
    _t(g, 362, y + 20, "ROMが素通しで読める", 7.6, GREEN, "start")
    _t(g, 362, y + 8, "（セーブ領域には届かない）", 6.8, GRAY, "start")
    g.add(Line(26, 108, W - 20, 108, strokeColor=RULE, strokeWidth=0.6))
    y2 = 56
    _box(g, 26, y2, 74, 30, "ROM", None, BGGRN, GREEN)
    _box(g, 140, y2, 82, 30, "SA-1", "起きてバスを握る", BGRED, RED)
    _box(g, 262, y2, 88, 30, "カートエッジ", None, colors.white, INK)
    g.add(Line(100, y2 + 15, 140, y2 + 15, strokeColor=RED, strokeWidth=1.9))
    g.add(Line(222, y2 + 15, 262, y2 + 15, strokeColor=RED, strokeWidth=1.9))
    _t(g, 20, y2 + 11, "クロック有り", 8, RED, "end")
    _t(g, 362, y2 + 20, "ROMはSA-1に握られる", 7.6, RED, "start")
    _t(g, 362, y2 + 8, "（かわりにセーブ領域が読める）", 6.8, GRAY, "start")
    _t(g, W / 2, 22, "この2つは両立しない。カート1番へのクロック配線1本で切り替わる。",
       7.6, GRAY)
    d.add(g)
    return d


def fig_cic():
    """図: CIC 1ビット＝372パルス"""
    d = Drawing(W, 156)
    g = Group()
    ox, oy = 66, 92
    total = W - 150
    _t(g, W / 2, 144, "CICは周波数ではなく「与えられたパルスの個数」で状態を進める",
       8.2, INK)
    g.add(Rect(ox, oy, total, 22, fillColor=BGBLUE, strokeColor=BLUE,
               strokeWidth=0.8))
    dw = total * 24.0 / 372
    g.add(Rect(ox, oy, dw, 22, fillColor=colors.HexColor("#cfe0ef"),
               strokeColor=BLUE, strokeWidth=0.8))
    sx = ox + total * 16.0 / 372
    g.add(Line(sx, oy - 6, sx, oy + 28, strokeColor=RED, strokeWidth=1.2))
    _t(g, sx, oy + 32, "読み位置 16", 6.8, RED)
    _t(g, ox + dw / 2, oy + 8, "駆動窓", 6.4, BLUE)
    _t(g, ox + dw + 8, oy + 8, "24パルス", 6.4, GRAY, "start")
    _t(g, ox + total / 2, oy - 16, "1ビット周期 ＝ 93命令 × 4 ＝ 372パルス",
       7.6, INK)
    _t(g, ox - 6, oy + 8, "1ビット", 7.2, INK, "end")
    ry = 34
    g.add(Rect(ox, ry, total, 18, fillColor=BG, strokeColor=RULE,
               strokeWidth=0.7))
    _t(g, ox + total / 2, ry + 5, "1ラウンド ＝ (16 − k) ビット。kはラウンドごとに変わる",
       7.4, RED)
    _t(g, ox - 6, ry + 5, "1ラウンド", 7.2, INK, "end")
    _t(g, ox + total / 2, ry - 14,
       "「全ラウンド15ビット固定」という前提が根本的な誤りだった", 7.2, GRAY)
    d.add(g)
    return d


def fig_ffrate():
    """図: 連続バンクモードで「62バンク一致」したときの0xFF率"""
    d = Drawing(W, 196)
    g = Group()
    ox, oy, pw, ph = 62, 44, W - 130, 100
    n = 64
    bw = pw / n
    vals = [0.0123, 0.5170] + [1.0] * 62
    for i, v in enumerate(vals):
        col = GREEN if v < 0.10 else (AMBER if v < 0.9 else RED)
        g.add(Rect(ox + i * bw, oy, bw * 0.86, ph * v, fillColor=col,
                   strokeColor=col))
    g.add(Line(ox, oy, ox + pw, oy, strokeColor=INK, strokeWidth=0.8))
    g.add(Line(ox, oy, ox, oy + ph, strokeColor=INK, strokeWidth=0.8))
    for v, lab in ((0, "0"), (0.5, "0.5"), (1.0, "1.0")):
        yy = oy + ph * v
        g.add(Line(ox - 3, yy, ox, yy, strokeColor=GRAY, strokeWidth=0.5))
        _t(g, ox - 6, yy - 2.5, lab, 6.6, GRAY, "end", MINCHO)
    for i, lab in ((0, "$C0"), (16, "$D0"), (32, "$E0"), (48, "$F0"),
                   (63, "$FF")):
        _t(g, ox + i * bw + bw / 2, oy - 12, lab, 6.4, GRAY)
    _t(g, ox - 6, oy + ph + 10, "0xFF率", 7, GRAY, "end")
    _t(g, ox + pw / 2, oy - 26, "バンク（$C0-$FF、4MB）", 7.2, GRAY)
    _t(g, W / 2, 188, "「相互相違0.00〜1.84%・62/64バンク一致」の内訳", 8.2, INK)
    _t(g, W / 2, 170, "62バンクが揃って0xFF一色。だから「一致率」は高く見えた",
       7.6, RED)
    _t(g, W / 2, 158, "実データがあるのは先頭128KB（$C0-$C1）だけ", 7.2, GRAY)
    d.add(g)
    return d


def fig_flow():
    """図: 「読むたび内容が変わる」の切り分け手順"""
    d = Drawing(W, 250)
    g = Group()
    cx = W / 2
    _box(g, cx - 105, 214, 210, 28, "症状：読むたび内容が変わる", None,
         colors.white, INK)
    _box(g, cx - 118, 158, 236, 34, "タイミング段階を5段掃引し、",
         "各段で2回読んで相違と0xFF率を記録", BGBLUE, BLUE)
    _arrow(g, cx, 214, cx, 192, INK)
    outs = [
        (10, 84, "相違が単調に減る", "0xFF率は不動", "タイミング不足",
         "遅い段階で読めば通る", BGGRN, GREEN),
        (185, 84, "相違が段階に無関係", "0xFF率が跳ね回る", "接触不良",
         "挿し直す／票を積む", BGAMB, AMBER),
        (360, 84, "遅くすると悪化する", "0xFF率も悪化", "電源不足",
         "外部電源を足す", BGRED, RED),
    ]
    for x, y, c1, c2, name, act, fill, edge in outs:
        wbox = 155
        g.add(Rect(x, y, wbox, 46, fillColor=colors.white, strokeColor=RULE,
                   strokeWidth=0.7))
        _t(g, x + wbox / 2, y + 30, c1, 7.2, INK)
        _t(g, x + wbox / 2, y + 16, c2, 7.2, INK)
        g.add(Rect(x, y - 42, wbox, 34, fillColor=fill, strokeColor=edge,
                   strokeWidth=1.0))
        _t(g, x + wbox / 2, y - 20, name, 8.4, edge)
        _t(g, x + wbox / 2, y - 33, act, 6.8, GRAY)
        _arrow(g, cx, 158, x + wbox / 2, y + 50, INK, 0.7)
        _arrow(g, x + wbox / 2, y, x + wbox / 2, y - 6, edge, 0.9)
    _t(g, W / 2, 6, "同じ症状に同じ原因を当てはめると必ず間違える。2つの指標で分ける。",
       7.6, GRAY)
    d.add(g)
    return d


def fig_window():
    """図: 電源投入直後の「窓」の実測"""
    d = Drawing(W, 178)
    g = Group()
    trials = [
        ("試行1", [0.55, 0, 0, 0, 0, 0, 0, 0]),
        ("試行2", [0.63, 0, 0, 0, 0, 0, 0, 0]),
        ("試行3", [0.92, 0.75, 0, 0, 0, 0, 0, 0]),
    ]
    ox, oy = 78, 42
    cw, ch = 46, 26
    for r, (lab, vals) in enumerate(trials):
        y = oy + (2 - r) * (ch + 5)
        _t(g, ox - 8, y + 9, lab, 7.4, INK, "end")
        for i, v in enumerate(vals):
            col = (colors.HexColor("#2e7d32") if v > 0.7 else
                   colors.HexColor("#7cb47f") if v > 0.4 else
                   colors.HexColor("#e6e2da"))
            g.add(Rect(ox + i * (cw + 3), y, cw, ch, fillColor=col,
                       strokeColor=colors.white, strokeWidth=1))
            _t(g, ox + i * (cw + 3) + cw / 2, y + 9,
               ("%.2f" % v) if v else "0.00", 6.8,
               colors.white if v > 0.4 else GRAY, "middle", MINCHO)
    for i in range(8):
        _t(g, ox + i * (cw + 3) + cw / 2, oy - 12, "%d本目" % (i + 1), 6.4,
           GRAY)
    _t(g, W / 2, 168, "電源投入直後に8バンクを続けて読んだときの実データ率", 8.2,
       INK)
    _t(g, W / 2, 150,
       "1バンク約1.5秒。開いているのは1〜2バンク分＝数秒しかない。", 7.4, RED)
    d.add(g)
    return d
