package com.shvc.sfcdumper

/**
 * ダンプ全体の流れ。PC版 gui/sfc_dumper_gui.py の判定〜全バンク読み出しに相当。
 * 呼び出し側はバックグラウンドスレッドから使うこと。
 */
object DumpJob {

    data class IdentifyResult(
        val mapping: RomAnalyzer.Mapping,
        val header: RomAnalyzer.Header?,
        val reason: String,
        val rawBank0: ByteArray,
        val totalBanks: Int,
    )

    /**
     * バンク0を読んでカートを判定する。
     *
     * ROM容量はヘッダの size_kb (1<<n KB) から求める。ヘッダが読めない場合は
     * 呼び出し側でユーザーに手動指定させる。
     */
    fun identify(
        reader: CartReader,
        log: (String) -> Unit,
        cancelled: () -> Boolean,
        onProgress: (got: Int, total: Int) -> Unit,
    ): IdentifyResult? {
        log("バンク0を読み出してカートを判定します...")
        val r = reader.readBankConfirmed(
            bank = 0, totalBanks = 0, startIdx = 0,
            cancelled = cancelled, onProgress = onProgress
        )
        val raw0 = r.data
        if (raw0 == null) {
            log("バンク0を安定して読めませんでした。接触・配線を確認してください。")
            return null
        }
        log("  バンク0 読み出し成功 [${r.tierLabel}]")

        val det = RomAnalyzer.detectMapping(raw0)
        log("  マッピング判定: ${det.mapping} (${det.reason})")

        val hdr = det.header
        if (hdr != null) {
            log("  タイトル: ${hdr.title}")
            log("  ROM容量: ${hdr.sizeKb ?: "不明"} KB / SRAM: ${hdr.sramBytes} バイト")
            log("  チェックサム(ヘッダ記載): $%04X".format(hdr.checksum))
        } else {
            log("  ヘッダを読めませんでした(容量は手動指定が必要)")
        }

        val totalBanks = bankCountFor(hdr?.sizeKb, det.mapping)
        if (totalBanks > 0) {
            log("  読み出すバンク数: $totalBanks")
        }
        return IdentifyResult(det.mapping, hdr, det.reason, raw0, totalBanks)
    }

    /**
     * ROM容量(KB)とマッピングから必要なバンク数を求める。
     *
     * LoROMは1バンクあたり32KBしかROMが現れないので、容量KB/32。
     * HiROMは1バンク64KBまるごとROMなので、容量KB/64。
     */
    fun bankCountFor(sizeKb: Int?, mapping: RomAnalyzer.Mapping): Int {
        if (sizeKb == null || sizeKb <= 0) return 0
        return if (mapping == RomAnalyzer.Mapping.LOROM) sizeKb / 32 else sizeKb / 64
    }

    data class DumpResult(
        val rom: ByteArray,
        val mapping: RomAnalyzer.Mapping,
        val checksum: RomAnalyzer.ChecksumResult,
    )

    /**
     * 全バンクを読み出してROM本体を組み立てる。
     *
     * AdaptiveTiming で「このカートに必要な速度段階」を引き継ぎながら進めるので、
     * 遅いカートでも毎バンク最速から試し直す無駄が出ない。
     */
    fun dumpAll(
        reader: CartReader,
        mapping: RomAnalyzer.Mapping,
        totalBanks: Int,
        log: (String) -> Unit,
        cancelled: () -> Boolean,
        onBankProgress: (bank: Int, totalBanks: Int, got: Int, bankSize: Int) -> Unit,
    ): DumpResult? {
        val adaptive = AdaptiveTiming()
        val raw = ByteArray(totalBanks * CartReader.BANK_SIZE)

        for (bank in 0 until totalBanks) {
            if (cancelled()) throw CartReader.Cancelled()
            log("バンク $bank / $totalBanks を読み出し中...")

            val startIdx = adaptive.nextStartIdx()
            val r = reader.readBankConfirmed(
                bank = bank,
                totalBanks = totalBanks,
                startIdx = startIdx,
                cancelled = cancelled,
                onProgress = { got, size -> onBankProgress(bank, totalBanks, got, size) },
            )
            val data = r.data
            if (data == null) {
                log("バンク $bank をどの速度段階でも安定して読めませんでした。中止します。")
                return null
            }
            adaptive.report(r.tierIdx!!, log)
            System.arraycopy(data, 0, raw, bank * CartReader.BANK_SIZE, CartReader.BANK_SIZE)
        }

        log("全バンクの読み出し完了。ROM本体を取り出します...")
        val rom = RomAnalyzer.extractRom(raw, mapping)
        val chk = RomAnalyzer.verifyChecksum(rom, mapping)
        if (chk.computed != null && chk.expected != null) {
            log(
                "チェックサム: 実測 $%04X / ヘッダ $%04X %s".format(
                    chk.computed, chk.expected, if (chk.ok) "[一致]" else "[不一致]"
                )
            )
        } else {
            log("チェックサム: ヘッダを読めず検証できませんでした")
        }
        return DumpResult(rom, mapping, chk)
    }
}
