package com.shvc.sfcdumper

import java.nio.charset.Charset

/**
 * カートから読んだ生データの解析。
 * PC版 gui/sfc_dumper_gui.py の extract_rom / read_header / detect_mapping /
 * verify_checksum と同じロジック。
 */
object RomAnalyzer {

    const val BANK_SIZE = 65536

    enum class Mapping { LOROM, HIROM }

    data class Header(
        val title: String,
        val mapMode: Int,
        val sizeKb: Int?,
        val sramBytes: Int,
        val checksum: Int,
        val printable: Int,
    )

    data class Detection(
        val mapping: Mapping,
        val header: Header?,
        val reason: String,
    )

    private val SHIFT_JIS: Charset = Charset.forName("Shift_JIS")

    /**
     * 生の64KB×Nバンクから、ROM本体を取り出す。
     *
     * LoROMではROMは各バンクの $8000-$FFFF にしか現れない。カートによっては下位32KBが
     * 上位のミラーになるが、下位が一切駆動されず 0x00 で読めるカートもある。
     * 上位32KBを採るのが常に正しい。
     */
    fun extractRom(raw: ByteArray, mapping: Mapping): ByteArray {
        if (mapping == Mapping.HIROM) return raw
        val half = BANK_SIZE / 2
        val out = ByteArray(raw.size / 2)
        var dst = 0
        var i = 0
        while (i < raw.size) {
            val end = minOf(i + BANK_SIZE, raw.size)
            val from = i + half
            if (from < end) {
                System.arraycopy(raw, from, out, dst, end - from)
                dst += end - from
            }
            i += BANK_SIZE
        }
        return if (dst == out.size) out else out.copyOf(dst)
    }

    /** 指定オフセットのヘッダを読む。妥当そうなら Header、駄目なら null。 */
    fun readHeader(rom: ByteArray, off: Int): Header? {
        if (rom.size < off + 32) return null
        val title = rom.copyOfRange(off, off + 21)
        val mapMode = rom[off + 0x15].toInt() and 0xFF
        val romSizeByte = rom[off + 0x17].toInt() and 0xFF
        val sramSizeByte = rom[off + 0x18].toInt() and 0xFF
        val complement = (rom[off + 28].toInt() and 0xFF) or ((rom[off + 29].toInt() and 0xFF) shl 8)
        val checksum = (rom[off + 30].toInt() and 0xFF) or ((rom[off + 31].toInt() and 0xFF) shl 8)
        if (((checksum + complement) and 0xFFFF) != 0xFFFF || checksum == 0) return null

        val printable = title.count { val v = it.toInt() and 0xFF; v in 0x20..0x7E }
        // SRAM容量は 1024 << n。0なら電池バックアップ無し。異常に大きい値はヘッダ誤読なので捨てる
        val sramBytes = if (sramSizeByte in 1..12) (1024 shl sramSizeByte) else 0

        return Header(
            title = String(title, SHIFT_JIS).trim(),
            mapMode = mapMode,
            sizeKb = if (romSizeByte < 20) (1 shl romSizeByte) else null,
            sramBytes = sramBytes,
            checksum = checksum,
            printable = printable,
        )
    }

    /**
     * バンク0の生データ(64KB)から マッピングとヘッダを判定する。
     *
     * ヘッダの map_mode バイトだけを見ると誤りやすいので、まず下位32KBの状態で決める。
     * LoROMではROMが $8000-$FFFF にしか出ないため、下位32KBは
     * 「上位のミラー」か「まったく駆動されず0x00」のどちらかになる。
     * HiROMは64KBフルに別データが載る。
     */
    fun detectMapping(rawBank0: ByteArray): Detection {
        val half = BANK_SIZE / 2
        val lo = rawBank0.copyOfRange(0, half)
        val hi = rawBank0.copyOfRange(half, BANK_SIZE)
        val hdr = readHeader(rawBank0, 0xFFC0)

        if (lo.contentEquals(hi)) {
            return Detection(Mapping.LOROM, hdr, "下位32KBが上位のミラー")
        }
        if (lo.all { it.toInt() == 0 }) {
            return Detection(Mapping.LOROM, hdr, "下位32KBが全て0x00(駆動されていない)")
        }
        return Detection(Mapping.HIROM, hdr, "下位32KBに独自データあり")
    }

    data class ChecksumResult(val ok: Boolean, val computed: Int?, val expected: Int?)

    fun verifyChecksum(rom: ByteArray, mapping: Mapping): ChecksumResult {
        val off = if (mapping == Mapping.HIROM) 0xFFC0 else 0x7FC0
        val hdr = readHeader(rom, off) ?: return ChecksumResult(false, null, null)
        var sum = 0
        for (b in rom) sum += (b.toInt() and 0xFF)
        val computed = sum and 0xFFFF
        return ChecksumResult(computed == hdr.checksum, computed, hdr.checksum)
    }
}
