package com.shvc.sfcdumper

import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.util.SerialInputOutputManager
import java.io.IOException
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Nano-2(マスタ)とのUSBシリアル通信。
 * PC版 host/bankio.py と同じプロトコル・同じタイミング制御。
 *
 * プロトコル:
 *   1. Nanoからの準備完了サイン 'R' (1バイト) を待つ
 *   2. 10バイトのヘッダを送る
 *      [bank, total_banks, rd_us(2), addr_us(2), pulse_us(2), flags, clock_ocr]
 *   3. (CICモード時のみ) 8バイトの診断情報を受け取る
 *   4. 65536バイト(1バンク)を受信
 *
 * 重要: ボーレート(1,000,000)はファーム側と一致させること。
 */
class CartReader(
    private val port: UsbSerialPort,
    private val log: (String) -> Unit = {},
) {
    companion object {
        const val BAUD_RATE = 1000000
        const val BANK_SIZE = RomAnalyzer.BANK_SIZE

        /** (rd_settle_us, addr_settle_us, pulse_us, ラベル) */
        val TIMING_TIERS = listOf(
            Tier(5, 5, 3, "高速"),
            Tier(20, 20, 5, "やや低速"),
            Tier(50, 50, 10, "低速"),
            Tier(100, 100, 20, "安全"),
            Tier(300, 300, 50, "最安全"),
        )

        /** 各段階で何回失敗したら次に上げるか */
        const val ATTEMPTS_PER_TIER = 3
    }

    data class Tier(val rdUs: Int, val addrUs: Int, val pulseUs: Int, val label: String)

    class Cancelled : Exception()

    // 受信データは「バイト単位」ではなく「チャンク(届いた配列)単位」でキューに積む。
    //
    // 以前は LinkedBlockingQueue<Byte> に1バイトずつ詰めていたが、これは1バイトごとに
    // Byteオブジェクトを生成するため、64KBのバンク1本で65536回のボクシングが発生する。
    // 1,000,000bps(毎秒10万バイト)ではこの処理がUSB受信スレッドの足を引っ張り、
    // 取りこぼしが起きて「59642/65536で止まる」現象になっていた。
    // チャンク単位ならonNewDataは配列を1個積むだけで済み、受信スレッドを塞がない。
    private val rxChunks = LinkedBlockingQueue<ByteArray>()
    private var curChunk: ByteArray? = null
    private var curPos = 0

    private val ioManager = SerialInputOutputManager(port, object : SerialInputOutputManager.Listener {
        override fun onNewData(data: ByteArray) {
            // ライブラリがバッファを使い回す実装の場合に備えてコピーしてから積む
            rxChunks.offer(data.copyOf())
        }
        override fun onRunError(e: Exception) {
            log("受信スレッドエラー: ${e.message}")
        }
    })

    init {
        // 既定のままだと1Mbpsでは1回の読み出しあたりの容量が足りず取りこぼしやすい
        ioManager.readBufferSize = 16384
        ioManager.start()
    }

    fun stopIo() {
        ioManager.stop()
    }

    private fun drainRx() {
        rxChunks.clear()
        curChunk = null
        curPos = 0
    }

    /**
     * DTRをトグルしてNanoをハードウェアリセットする。
     *
     * Arduinoの自動リセットは、DTR線がコンデンサ経由で/RESETに繋がっていて、
     * DTRをアサートした瞬間のパルスでリセットがかかる仕組み。
     * PC版(pyserial)はポートを開くたびにこれが自動で起きていた。
     */
    private fun resetNano() {
        try {
            port.setDTR(false)
            Thread.sleep(60)
            port.setDTR(true)
            Thread.sleep(60)
        } catch (e: UnsupportedOperationException) {
            // DTR制御に対応していないチップでは何もできない
            log("    このUSBシリアルチップはDTR制御に対応していません")
        }
    }

    private fun writeAll(bytes: ByteArray, timeoutMs: Int = 5000) {
        // USBフルスピードのバルク転送は1パケット最大64バイト。境界値ちょうどで
        // 送るとデバイス側が確定させず止まることがあるため小分けにする。
        var offset = 0
        while (offset < bytes.size) {
            val end = minOf(offset + 32, bytes.size)
            port.write(bytes.copyOfRange(offset, end), timeoutMs)
            offset = end
        }
    }

    /**
     * dest[offset] から count バイト埋まるまで待つ。
     * 全部埋まれば true、タイムアウトしたら false(その時点までは書き込まれている)。
     * 何バイト受け取れたかは filled で返す。
     */
    private fun readFully(
        dest: ByteArray, offset: Int, count: Int, timeoutMs: Long,
        onChunk: ((filled: Int) -> Unit)? = null,
        cancelled: (() -> Boolean)? = null,
    ): Int {
        var need = count
        var off = offset
        var filled = 0
        val deadline = System.currentTimeMillis() + timeoutMs
        while (need > 0) {
            if (cancelled?.invoke() == true) throw Cancelled()
            var c = curChunk
            if (c == null || curPos >= c.size) {
                val remaining = deadline - System.currentTimeMillis()
                if (remaining <= 0) return filled
                c = rxChunks.poll(remaining, TimeUnit.MILLISECONDS) ?: return filled
                curChunk = c
                curPos = 0
            }
            val take = minOf(c.size - curPos, need)
            System.arraycopy(c, curPos, dest, off, take)
            curPos += take
            off += take
            need -= take
            filled += take
            onChunk?.invoke(filled)
        }
        return filled
    }

    /** 1バイト読む。タイムアウトしたら null。 */
    private fun readByteOrNull(timeoutMs: Long): Int? {
        val one = ByteArray(1)
        if (readFully(one, 0, 1, timeoutMs) != 1) return null
        return one[0].toInt() and 0xFF
    }

    private fun readExact(n: Int, timeoutMs: Long): ByteArray? {
        val result = ByteArray(n)
        if (readFully(result, 0, n, timeoutMs) != n) return null
        return result
    }

    /**
     * 指定タイミングで1バンクを1回読む。失敗したら null。
     *
     * totalBanks は読み出しには使わず、OLEDに「現在/全体」を表示させるためだけに送る。
     */
    fun readBankOnce(
        bank: Int,
        tier: Tier,
        totalBanks: Int = 0,
        sram: Boolean = false,
        holdReset: Boolean = false,
        cartClock: Boolean = false,
        cic: Boolean = false,
        clockOcr: Int = 7,
        prime: Boolean = false,
        cancelled: (() -> Boolean)? = null,
        onProgress: ((got: Int, total: Int) -> Unit)? = null,
    ): ByteArray? {
        // ファーム(nano2_master.ino)は setup() の中で 'R' を送り、1バンク送ったら
        // loop() は空で何もしない「使い切り」の設計になっている。
        // PC版が読み出しのたびに serial.Serial() でポートを開き直していたのは、
        // その際のDTR操作でNanoがリセットされ setup() が再実行されることを
        // 利用していたため。こちらはポートを開いたままなので、明示的に
        // DTRをトグルしてリセットをかけないと2回目以降の 'R' が来ない。
        resetNano()
        drainRx()

        // 準備完了サイン 'R' を待つ(ブートローダの起動待ちがあるため長めに取る)
        val deadline = System.currentTimeMillis() + 20000
        var ready = false
        while (System.currentTimeMillis() < deadline) {
            if (cancelled?.invoke() == true) throw Cancelled()
            val b = readByteOrNull(500) ?: continue
            if (b == 'R'.code) { ready = true; break }
        }
        if (!ready) {
            log("    Nanoからの準備完了(R)が来ませんでした")
            return null
        }

        val flags = (if (sram) 0x01 else 0x00) or
            (if (holdReset) 0x02 else 0x00) or
            (if (cartClock) 0x04 else 0x00) or
            (if (cic) 0x08 else 0x00) or
            (if (prime) 0x20 else 0x00)

        val header = byteArrayOf(
            bank.toByte(),
            (totalBanks and 0xFF).toByte(),
            (tier.rdUs and 0xFF).toByte(), ((tier.rdUs shr 8) and 0xFF).toByte(),
            (tier.addrUs and 0xFF).toByte(), ((tier.addrUs shr 8) and 0xFF).toByte(),
            (tier.pulseUs and 0xFF).toByte(), ((tier.pulseUs shr 8) and 0xFF).toByte(),
            flags.toByte(),
            (clockOcr and 0xFF).toByte(),
        )
        writeAll(header)

        if (cic) {
            val st = readExact(8, 5000)
            if (st == null) {
                log("    CIC: 応答なし")
            } else {
                // 遷移0回でHigh維持なら本物。振動していればリセットループ=認証失敗。
                fun v(x: Int) = x * 4 * 5.0 / 1023
                val names = listOf(0 to "10番 本体リセット出力", 4 to "1番 CICデータ線")
                for ((k, name) in names) {
                    val lo = st[k].toInt() and 0xFF
                    val hi = st[k + 1].toInt() and 0xFF
                    val tr = st[k + 2].toInt() and 0xFF
                    val verdict = when {
                        tr == 0 && hi > 200 -> "High固定"
                        tr == 0 -> "Low固定"
                        else -> "振動${tr}回"
                    }
                    log("    CIC[$name] %.2f〜%.2fV %s".format(v(lo), v(hi), verdict))
                }
            }
        }

        val buf = ByteArray(BANK_SIZE)
        var lastReported = 0
        val got = readFully(
            buf, 0, BANK_SIZE, 30000,
            onChunk = { filled ->
                if (filled - lastReported >= 4096 || filled == BANK_SIZE) {
                    lastReported = filled
                    onProgress?.invoke(filled, BANK_SIZE)
                }
            },
            cancelled = cancelled,
        )
        if (got != BANK_SIZE) {
            log("    タイムアウト ($got/$BANK_SIZE)")
            return null
        }
        onProgress?.invoke(BANK_SIZE, BANK_SIZE)
        return buf
    }

    /**
     * 全バイトが同一(0x00や0xFF等)かどうか。
     *
     * 「2回読んで一致」だけでは、カートが外れて何も読めていない状態を検出できない。
     * そういう出力は自分自身と必ず一致してしまうため。
     * 実データがこうなる確率は天文学的に低いので、これが出たら即座に異常とみなす。
     */
    private fun isDegenerate(data: ByteArray): Boolean {
        val first = data[0]
        return data.all { it == first }
    }

    data class ConfirmResult(val data: ByteArray?, val tierIdx: Int?, val tierLabel: String?)

    /**
     * バンクを読み、2回連続で完全一致するまで繰り返す。
     *
     * startIdx で TIMING_TIERS の何番目から試すかを指定できる。前のバンクで分かって
     * いる「このカートに必要な段階」から始めれば、通らないと分かっている速い段階を
     * 無駄に試さずに済む(AdaptiveTiming参照)。
     */
    fun readBankConfirmed(
        bank: Int,
        totalBanks: Int = 0,
        startIdx: Int = 0,
        holdReset: Boolean = false,
        cancelled: (() -> Boolean)? = null,
        onProgress: ((got: Int, total: Int) -> Unit)? = null,
    ): ConfirmResult {
        for (idx in startIdx until TIMING_TIERS.size) {
            val tier = TIMING_TIERS[idx]
            var prev: ByteArray? = null
            for (attempt in 1..ATTEMPTS_PER_TIER) {
                if (cancelled?.invoke() == true) throw Cancelled()
                val data = readBankOnce(
                    bank, tier, totalBanks, holdReset = holdReset,
                    cancelled = cancelled, onProgress = onProgress
                ) ?: continue

                if (prev != null) {
                    var diff = 0
                    for (i in data.indices) if (prev[i] != data[i]) diff++
                    if (diff == 0) {
                        if (isDegenerate(data)) {
                            log("    [${tier.label}] 一致はしたが全バイトが同一値" +
                                "(0x%02x)。カートが外れている可能性。異常として扱います"
                                    .format(data[0].toInt() and 0xFF))
                        } else {
                            return ConfirmResult(data, idx, tier.label)
                        }
                    } else {
                        log("    [${tier.label}] 試行$attempt: 前回と $diff バイト相違")
                    }
                }
                prev = data
            }
            log("    [${tier.label}] ${ATTEMPTS_PER_TIER}回で一致せず、次の段階に上げます")
        }
        return ConfirmResult(null, null, null)
    }
}

/**
 * 複数バンクにまたがって「今どの段階が必要か」を記憶する。
 *
 * 毎回 高速(tier0) から試すと、遅いカートでは「どうせ通らないと分かっている段階」に
 * 毎回時間を捨てることになる。そこで基本は「前回成功した段階」から始めて無駄な
 * 足踏みを無くしつつ、probeEvery バンクに1回だけ最速から試し、通れば速い設定に戻す。
 * カートが温まって安定した等で条件が良くなった場合に自動で速度を取り戻すため。
 */
class AdaptiveTiming(private val probeEvery: Int = 5) {
    var tierIdx: Int = 0
        private set
    private var banksSinceProbe = 0

    fun nextStartIdx(): Int {
        if (tierIdx > 0 && banksSinceProbe >= probeEvery) return 0
        return tierIdx
    }

    fun report(newTierIdx: Int, log: ((String) -> Unit)? = null) {
        val probed = tierIdx > 0 && banksSinceProbe >= probeEvery
        if (probed && newTierIdx < tierIdx) {
            log?.invoke(
                "  高速側へ復帰: 段階を ${CartReader.TIMING_TIERS[tierIdx].label} → " +
                    "${CartReader.TIMING_TIERS[newTierIdx].label} に戻しました"
            )
        }
        tierIdx = newTierIdx
        banksSinceProbe = if (probed) 0 else banksSinceProbe + 1
    }
}
