package com.shvc.sfcdumper

import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.SharedPreferences
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.widget.Button
import android.widget.ProgressBar
import android.widget.RadioButton
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.documentfile.provider.DocumentFile
import com.hoho.android.usbserial.driver.UsbSerialPort
import com.hoho.android.usbserial.driver.UsbSerialProber
import java.io.IOException

class MainActivity : AppCompatActivity() {

    companion object {
        private const val ACTION_USB_PERMISSION = "com.shvc.sfcdumper.USB_PERMISSION"
        private const val PREFS_NAME = "sfcdumper_prefs"
        private const val PREF_FOLDER_URI = "output_folder_uri"
    }

    private lateinit var prefs: SharedPreferences

    private lateinit var btnConnect: Button
    private lateinit var tvConnStatus: TextView
    private lateinit var btnPickFolder: Button
    private lateinit var tvFolderPath: TextView
    private lateinit var tvCartInfo: TextView
    private lateinit var radioAuto: RadioButton
    private lateinit var radioLoRom: RadioButton
    private lateinit var radioHiRom: RadioButton
    private lateinit var btnIdentify: Button
    private lateinit var btnDump: Button
    private lateinit var btnCancel: Button
    private lateinit var tvProgressLabel: TextView
    private lateinit var progressDump: ProgressBar
    private lateinit var btnCopyLog: Button
    private lateinit var scrollLog: ScrollView
    private lateinit var tvLog: TextView

    private var usbPort: UsbSerialPort? = null
    private var reader: CartReader? = null

    private var outputFolder: Uri? = null
    private var lastIdentify: DumpJob.IdentifyResult? = null

    private var worker: Thread? = null
    @Volatile private var cancelRequested = false

    private val usbReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context, intent: Intent) {
            if (intent.action != ACTION_USB_PERMISSION) return
            val device = intent.getUsbDeviceExtra()
            if (intent.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) {
                if (device != null) connectToDevice(device)
            } else {
                appendLog("USB権限が拒否されました。")
            }
        }
    }

    private val folderPickerLauncher =
        registerForActivityResult(ActivityResultContracts.OpenDocumentTree()) { uri: Uri? ->
            if (uri != null) {
                contentResolver.takePersistableUriPermission(
                    uri,
                    Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_WRITE_URI_PERMISSION
                )
                prefs.edit().putString(PREF_FOLDER_URI, uri.toString()).apply()
                setOutputFolder(uri)
            }
        }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        prefs = getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        btnConnect = findViewById(R.id.btnConnect)
        tvConnStatus = findViewById(R.id.tvConnStatus)
        btnPickFolder = findViewById(R.id.btnPickFolder)
        tvFolderPath = findViewById(R.id.tvFolderPath)
        tvCartInfo = findViewById(R.id.tvCartInfo)
        radioAuto = findViewById(R.id.radioAuto)
        radioLoRom = findViewById(R.id.radioLoRom)
        radioHiRom = findViewById(R.id.radioHiRom)
        btnIdentify = findViewById(R.id.btnIdentify)
        btnDump = findViewById(R.id.btnDump)
        btnCancel = findViewById(R.id.btnCancel)
        tvProgressLabel = findViewById(R.id.tvProgressLabel)
        progressDump = findViewById(R.id.progressDump)
        btnCopyLog = findViewById(R.id.btnCopyLog)
        scrollLog = findViewById(R.id.scrollLog)
        tvLog = findViewById(R.id.tvLog)

        btnConnect.setOnClickListener { requestUsbConnection() }
        btnPickFolder.setOnClickListener { folderPickerLauncher.launch(null) }
        btnIdentify.setOnClickListener { onIdentifyClicked() }
        btnDump.setOnClickListener { onDumpClicked() }
        btnCancel.setOnClickListener { onCancelClicked() }
        btnCopyLog.setOnClickListener { copyLogToClipboard() }

        val filter = IntentFilter(ACTION_USB_PERMISSION)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(usbReceiver, filter, Context.RECEIVER_NOT_EXPORTED)
        } else {
            @Suppress("UnspecifiedRegisterReceiverFlag")
            registerReceiver(usbReceiver, filter)
        }

        prefs.getString(PREF_FOLDER_URI, null)?.let {
            try {
                setOutputFolder(Uri.parse(it))
            } catch (e: Exception) {
                appendLog("保存先の復元に失敗: ${e.message}")
            }
        }
    }

    override fun onDestroy() {
        super.onDestroy()
        try {
            unregisterReceiver(usbReceiver)
        } catch (e: IllegalArgumentException) {
            // 未登録なら何もしない
        }
        try {
            reader?.stopIo()
        } catch (e: Exception) {
            // 終了時なので無視してよい
        }
        try {
            usbPort?.close()
        } catch (e: IOException) {
            // 終了時なので無視してよい
        }
    }

    private fun Intent.getUsbDeviceExtra(): UsbDevice? {
        return if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            getParcelableExtra(UsbManager.EXTRA_DEVICE, UsbDevice::class.java)
        } else {
            @Suppress("DEPRECATION")
            getParcelableExtra(UsbManager.EXTRA_DEVICE)
        }
    }

    // ---------------------------------------------------------------- USB

    private fun requestUsbConnection() {
        val usbManager = getSystemService(Context.USB_SERVICE) as UsbManager
        val drivers = UsbSerialProber.getDefaultProber().findAllDrivers(usbManager)
        if (drivers.isEmpty()) {
            appendLog("USBシリアルデバイスが見つかりません。Nano-2(マスタ)が接続されているか確認してください。")
            return
        }
        val device = drivers[0].device
        if (usbManager.hasPermission(device)) {
            connectToDevice(device)
            return
        }
        val flags = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) PendingIntent.FLAG_MUTABLE else 0
        // Android 14以降、暗黙的インテントにFLAG_MUTABLEは禁止のため明示的にする
        val intent = Intent(ACTION_USB_PERMISSION).setPackage(packageName)
        usbManager.requestPermission(device, PendingIntent.getBroadcast(this, 0, intent, flags))
    }

    private fun connectToDevice(device: UsbDevice) {
        val usbManager = getSystemService(Context.USB_SERVICE) as UsbManager
        val driver = UsbSerialProber.getDefaultProber()
            .findAllDrivers(usbManager)
            .firstOrNull { it.device.deviceId == device.deviceId }
        if (driver == null) {
            appendLog("対応するUSBシリアルドライバが見つかりません。")
            return
        }
        val connection = usbManager.openDevice(driver.device)
        if (connection == null) {
            appendLog("USBデバイスを開けませんでした。権限を確認してください。")
            return
        }
        val port = driver.ports[0]
        try {
            port.open(connection)
            port.setParameters(
                CartReader.BAUD_RATE, 8, UsbSerialPort.STOPBITS_1, UsbSerialPort.PARITY_NONE
            )
            try {
                port.setDTR(true)
                port.setRTS(true)
            } catch (e: UnsupportedOperationException) {
                // DTR/RTS制御非対応のチップなら無視
            }
        } catch (e: IOException) {
            appendLog("接続エラー: ${e.message}")
            return
        }
        usbPort = port
        reader = CartReader(port, log = ::appendLogAsync)
        tvConnStatus.text = "接続済み (${CartReader.BAUD_RATE} bps)"
        appendLog("USB接続しました。")
    }

    // ------------------------------------------------------------ 保存先

    private fun setOutputFolder(uri: Uri) {
        outputFolder = uri
        tvFolderPath.text = uri.path ?: uri.toString()
    }

    // ------------------------------------------------------------ 操作

    private fun requireReady(): CartReader? {
        val r = reader
        if (r == null) {
            appendLog("先にUSB接続してください。")
            return null
        }
        if (worker?.isAlive == true) {
            appendLog("処理中です。完了までお待ちください。")
            return null
        }
        return r
    }

    private fun onIdentifyClicked() {
        val r = requireReady() ?: return
        cancelRequested = false
        setBusy(true)
        appendLog("=== カート判定 ===")

        worker = Thread {
            try {
                val result = DumpJob.identify(
                    r, ::appendLogAsync, { cancelRequested },
                    { got, total -> updateProgress("バンク0 読み出し中", got, total) }
                )
                runOnUiThread {
                    lastIdentify = result
                    if (result != null) showCartInfo(result)
                    setBusy(false)
                }
            } catch (e: CartReader.Cancelled) {
                runOnUiThread { appendLog("中止しました。"); setBusy(false) }
            } catch (e: Exception) {
                runOnUiThread { appendLog("エラー: ${e.message}"); setBusy(false) }
            }
        }
        worker?.start()
    }

    private fun showCartInfo(res: DumpJob.IdentifyResult) {
        val h = res.header
        tvCartInfo.text = buildString {
            append("マッピング: ${res.mapping} (${res.reason})\n")
            if (h != null) {
                append("タイトル: ${h.title}\n")
                append("ROM容量: ${h.sizeKb ?: "不明"} KB / SRAM: ${h.sramBytes} バイト\n")
                append("バンク数: ${res.totalBanks}")
            } else {
                append("ヘッダを読めませんでした")
            }
        }
    }

    private fun selectedMapping(): RomAnalyzer.Mapping? = when {
        radioLoRom.isChecked -> RomAnalyzer.Mapping.LOROM
        radioHiRom.isChecked -> RomAnalyzer.Mapping.HIROM
        else -> lastIdentify?.mapping
    }

    private fun onDumpClicked() {
        val r = requireReady() ?: return
        val folder = outputFolder
        if (folder == null) {
            appendLog("先に保存先フォルダを選択してください。")
            return
        }
        val mapping = selectedMapping()
        if (mapping == null) {
            appendLog("先に「カートを判定」を実行するか、LoROM/HiROMを手動で選んでください。")
            return
        }
        val id = lastIdentify
        val totalBanks = if (id != null && id.totalBanks > 0) {
            id.totalBanks
        } else {
            appendLog("ROM容量が不明のためバンク数を決められません。先に「カートを判定」を実行してください。")
            return
        }
        val title = id?.header?.title?.takeIf { it.isNotBlank() } ?: "dump"

        cancelRequested = false
        progressDump.progress = 0
        setBusy(true)
        appendLog("=== ダンプ開始 (${mapping}, $totalBanks バンク) ===")

        worker = Thread {
            try {
                val result = DumpJob.dumpAll(
                    r, mapping, totalBanks, ::appendLogAsync, { cancelRequested },
                    { bank, total, got, size ->
                        val overall = (bank * size + got).toLong()
                        val whole = (total.toLong() * size)
                        updateProgressOverall("バンク $bank/$total", overall, whole)
                    }
                )
                if (result != null) {
                    saveRom(folder, title, result)
                }
                runOnUiThread { setBusy(false) }
            } catch (e: CartReader.Cancelled) {
                runOnUiThread { appendLog("中止しました。"); setBusy(false) }
            } catch (e: Exception) {
                runOnUiThread { appendLog("エラー: ${e.message}"); setBusy(false) }
            }
        }
        worker?.start()
    }

    private fun saveRom(folder: Uri, title: String, result: DumpJob.DumpResult) {
        try {
            val safe = title.replace(Regex("[^A-Za-z0-9ぁ-んァ-ヶ一-龠 _-]"), "_").trim().ifBlank { "dump" }
            val tree = DocumentFile.fromTreeUri(this, folder)
            if (tree == null) {
                appendLogAsync("保存先フォルダを開けませんでした。")
                return
            }
            val file = tree.createFile("application/octet-stream", "$safe.sfc")
            if (file == null) {
                appendLogAsync("ファイルを作成できませんでした。")
                return
            }
            contentResolver.openOutputStream(file.uri)?.use { it.write(result.rom) }
            appendLogAsync("保存しました: $safe.sfc (${result.rom.size / 1024} KB)")
        } catch (e: Exception) {
            appendLogAsync("保存に失敗: ${e.message}")
        }
    }

    private fun onCancelClicked() {
        if (worker?.isAlive == true) {
            cancelRequested = true
            appendLog("中止を要求しました...")
        }
    }

    private fun setBusy(busy: Boolean) {
        btnConnect.isEnabled = !busy
        btnPickFolder.isEnabled = !busy
        btnIdentify.isEnabled = !busy
        btnDump.isEnabled = !busy
        btnCancel.isEnabled = busy
    }

    // --------------------------------------------------------- UIヘルパー

    private fun updateProgress(label: String, got: Int, total: Int) {
        if (total <= 0) return
        val pct = (got * 100L / total).toInt().coerceIn(0, 100)
        runOnUiThread {
            tvProgressLabel.text = "$label  $got / $total"
            progressDump.progress = pct
        }
    }

    private fun updateProgressOverall(label: String, done: Long, total: Long) {
        if (total <= 0) return
        val pct = (done * 100L / total).toInt().coerceIn(0, 100)
        runOnUiThread {
            tvProgressLabel.text = "$label  ${done / 1024} / ${total / 1024} KB"
            progressDump.progress = pct
        }
    }

    private fun appendLog(msg: String) {
        tvLog.append("$msg\n")
        scrollLog.post { scrollLog.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    private fun appendLogAsync(msg: String) {
        runOnUiThread { appendLog(msg) }
    }

    private fun copyLogToClipboard() {
        val clipboard = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
        clipboard.setPrimaryClip(ClipData.newPlainText("SFC-Dumper log", tvLog.text.toString()))
        Toast.makeText(this, "ログをコピーしました", Toast.LENGTH_SHORT).show()
    }
}
