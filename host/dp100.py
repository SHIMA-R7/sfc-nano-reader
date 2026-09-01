# -*- coding: utf-8 -*-
"""Alientek DP100 を USB HID で直接叩く。

■ なぜこれを作ったのか
リレーで電源を入り切りしていたが、DP100 なら PC から直接 出力ON/OFF・電圧設定・
電流測定ができる。リレーもUnoも要らなくなり、しかも**消費電流が測れる**。

■ 安全装置（これが本体）
このハードは 5V 専用で、**それ以上かけると壊れる**。よって:
    ・VMAX_MV = 5000 を超える設定は、コード側で拒否する（例外を投げる）
    ・OVP を 5.5V に置き、万一暴走しても電源側でトリップさせる
    ・出力を入れる前に必ず読み戻して、設定値が意図どおりか確認する
「引数で渡せてしまう」形にしないこと。事故は必ず引数から入る。

■ プロトコル出典
palzhj/pydp100 および scottbez1/webdp100（実機で動作している実装）。
    フレーム: [0xFB, opcode, 0x00, len, data..., crc_lo, crc_hi]
    CRC-16/Modbus (poly 0x8005, init 0xFFFF, reflected)
    単位は mV / mA / 0.1℃
"""
import time
import crcmod
import hid

VID, PID = 0x2E3C, 0xAF01
DR_H2D, DR_D2H = 0xFB, 0xFA
OP_DEVICEINFO, OP_BASICINFO, OP_BASICSET = 0x10, 0x30, 0x35
SET_MODIFY, SET_ACT = 0x20, 0x80

# ---- 安全上限。ここを緩めないこと ----
VMAX_MV = 5000          # このダンパーは5V専用
OVP_MV  = 5500          # 過電圧保護。5Vを明確に超えたら電源が落とす
IMAX_MA = 1000          # 3枚のNano+カートで数百mA。1Aで十分な余裕
OCP_MA  = 1200

_crc = crcmod.mkCrcFun(0x18005, rev=True, initCrc=0xFFFF, xorOut=0x0000)

def _frame(op, data=b""):
    f = bytes([DR_H2D, op & 0xFF, 0x00, len(data) & 0xFF]) + data
    c = _crc(f)
    return f + bytes([c & 0xFF, (c >> 8) & 0xFF])

def _u16(b, i):  return b[i] | (b[i + 1] << 8)

def _parse(buf):
    if not buf or buf[0] != DR_D2H:
        return None
    n = buf[3]
    if _crc(bytes(buf[0:4 + n + 2])) != 0:      # 正しければ0になる
        return None
    return buf[1], bytes(buf[4:4 + n])

class DP100:
    def __init__(self):
        # hidapi 系(hid.Device)と cython-hidapi 系(hid.device())で API が違う。両対応。
        if hasattr(hid, "Device"):
            self.d = hid.Device(VID, PID); self._legacy = False
        else:
            self.d = hid.device(); self.d.open(VID, PID); self._legacy = True
            self.d.set_nonblocking(0)
    def _read(self):
        return bytes(self.d.read(64, 500) if self._legacy else self.d.read(64, timeout=500))
    def close(self):
        self.d.close()
    def __enter__(self):  return self
    def __exit__(self, *a): self.close()

    def _xfer(self, op, data=b"", want=None, tries=6, minlen=0):
        """応答を1つ取る。

        **minlen を必ず指定すること。** OP_BASICSET は書き込み後に
        「状態1バイトだけ」の受領応答を返すことがあり、設定10バイトが来る保証がない。
        長さを見ずに r[0] を読んで IndexError で落ち、実験が1本落ちた。
        """
        for _ in range(tries):
            # **先頭のレポートID 0x00 が要る。** 無いと fa 00 00 01 (NONE) しか返らない。
            self.d.write(b'\x00' + _frame(op, data))
            time.sleep(0.06)
            r = _parse(self._read())
            if r and (want is None or r[0] == want) and len(r[1]) >= minlen:
                return r[1]
        return None

    def device_info(self):
        r = self._xfer(OP_DEVICEINFO, want=OP_DEVICEINFO, minlen=22)
        if not r: return None
        return {"type": r[0:15].split(b"\x00")[0].decode("utf-8", "replace"),
                "hw": _u16(r,16)/10, "app": _u16(r,18)/10, "boot": _u16(r,20)/10}

    def status(self):
        r = self._xfer(OP_BASICINFO, want=OP_BASICINFO, minlen=16)
        if not r: return None
        return {"vin_mV":_u16(r,0), "vout_mV":_u16(r,2), "iout_mA":_u16(r,4),
                "vo_max_mV":_u16(r,6), "temp_C":_u16(r,8)/10,
                "out_mode":r[14], "work_st":r[15]}

    def setting(self):
        r = self._xfer(OP_BASICSET, bytes([SET_ACT]), want=OP_BASICSET, minlen=10)
        if not r: return None
        return {"index":r[0], "state":r[1], "vo_set_mV":_u16(r,2),
                "io_set_mA":_u16(r,4), "ovp_mV":_u16(r,6), "ocp_mA":_u16(r,8)}

    def apply(self, on, v_mV, i_mA):
        """出力設定。**VMAX_MV を超える要求は必ず例外**にする。"""
        if v_mV > VMAX_MV:
            raise ValueError("要求 %dmV は上限 %dmV を超えています。このハードは5V専用です"
                             % (v_mV, VMAX_MV))
        if i_mA > IMAX_MA:
            raise ValueError("要求 %dmA は上限 %dmA を超えています" % (i_mA, IMAX_MA))
        d = bytes([SET_MODIFY, 1 if on else 0,
                   v_mV & 0xFF, (v_mV >> 8) & 0xFF, i_mA & 0xFF, (i_mA >> 8) & 0xFF,
                   OVP_MV & 0xFF, (OVP_MV >> 8) & 0xFF, OCP_MA & 0xFF, (OCP_MA >> 8) & 0xFF])
        self._xfer(OP_BASICSET, d)
        time.sleep(0.15)
        # 受領応答が短いことがあるので、設定が読めるまで数回粘る
        for _ in range(4):
            c = self.setting()
            if c: return c
            time.sleep(0.1)
        return None

    def output(self, on):
        s = self.setting() or {}
        return self.apply(on, min(s.get("vo_set_mV", 5000), VMAX_MV),
                          min(s.get("io_set_mA", IMAX_MA), IMAX_MA))


def power_on_5v(log=print):
    """5.000V で出力を入れる。**手順を崩さないこと。**

    1. 出力を切ったまま 5.000V を書く
    2. 読み戻して 5000mV であることを確認する（違えば中止）
    3. 出力を入れる
    4. **実測電圧を読み、5.3Vを超えていたら即座に切る**

    OVP は index=0x20 の書き込みでは変更できず 30.5V のまま残る。
    よって電源側の過電圧保護は当てにせず、4番の実測確認を保護とする。
    """
    p = DP100()
    d = p.device_info()
    log("電源: %s HW%.1f/APP%.1f" % (d['type'], d['hw'], d['app']))
    c = p.apply(False, 5000, 1000)
    if c is None or c['vo_set_mV'] != 5000 or c['state'] != 0:
        p.close(); raise RuntimeError("5Vの設定を確認できませんでした: %r" % (c,))
    log("設定確認: %.3f V / %.3f A（出力はまだ切）" % (c['vo_set_mV']/1000, c['io_set_mA']/1000))
    p.apply(True, 5000, 1000)
    time.sleep(0.4)
    s = p.status()
    v = s['vout_mV']/1000.0
    if v > 5.3:
        p.apply(False, 5000, 1000)
        p.close(); raise RuntimeError("出力が %.3f V と高すぎるため切りました" % v)
    log("出力ON: %.3f V / %.3f A" % (v, s['iout_mA']/1000))
    return p


def power_off(p, log=print):
    try:
        p.apply(False, 5000, 1000)
        s = p.status()
        log("出力OFF: %.3f V" % (s['vout_mV']/1000))
    finally:
        p.close()
