package com.xjtlu.energyagent

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.text.InputType
import android.view.View
import android.widget.Button
import android.widget.EditText
import android.widget.ProgressBar
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.export.CsvExporter
import com.xjtlu.energyagent.run.ExperimentPlan
import com.xjtlu.energyagent.run.RunEngine
import com.xjtlu.energyagent.run.WorkloadEngine
import com.xjtlu.energyagent.service.ExperimentService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File

/**
 * Task detail screen. Shows the task metadata and a single「开始任务」button.
 * Power / RSSI / chart / phase progress are revealed only after the task is
 * started (RunEngine state leaves IDLE). The current phase and its elapsed /
 * remaining time drive the progress bar.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var taskTitle: TextView
    private lateinit var taskMeta: TextView
    private lateinit var taskPhases: TextView
    private lateinit var taskHint: TextView

    private lateinit var phaseSection: View
    private lateinit var phaseLabel: TextView
    private lateinit var phaseTime: TextView
    private lateinit var phaseProgress: ProgressBar

    private lateinit var metricButtons: View
    private lateinit var status: TextView
    private lateinit var chart: TelemetryChart

    private lateinit var btnStart: Button
    private lateinit var btnStop: Button
    private lateinit var btnExport: Button

    private var taskJson: JSONObject? = null
    private var experimentId: String = ""

    private val handler = Handler(Looper.getMainLooper())
    private val refreshLoop = object : Runnable {
        override fun run() {
            refresh()
            handler.postDelayed(this, 500)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        taskTitle = findViewById(R.id.task_title)
        taskMeta = findViewById(R.id.task_meta)
        taskPhases = findViewById(R.id.task_phases)
        taskHint = findViewById(R.id.task_hint)

        phaseSection = findViewById(R.id.phase_section)
        phaseLabel = findViewById(R.id.phase_label)
        phaseTime = findViewById(R.id.phase_time)
        phaseProgress = findViewById(R.id.phase_progress)

        metricButtons = findViewById(R.id.metric_buttons)
        status = findViewById(R.id.status)
        chart = findViewById(R.id.chart)

        btnStart = findViewById(R.id.btn_start)
        btnStop = findViewById(R.id.btn_stop)
        btnExport = findViewById(R.id.btn_export)

        requestRuntimePermissions()

        findViewById<Button>(R.id.btn_back).setOnClickListener { finish() }
        findViewById<Button>(R.id.btn_help).setOnClickListener { showHelp() }
        findViewById<Button>(R.id.btn_settings).setOnClickListener { showNoSignalSettings() }

        experimentId = intent.getStringExtra(EXTRA_EXPERIMENT_ID)
            ?: AgentState.currentPlan?.experimentId.orEmpty()

        if (!loadTask()) {
            Toast.makeText(this, R.string.task_load_failed, Toast.LENGTH_SHORT).show()
            finish()
            return
        }

        val metricButtonsList = listOf(
            R.id.btn_metric_power to TelemetryChart.Metric.POWER,
            R.id.btn_metric_current to TelemetryChart.Metric.CURRENT,
            R.id.btn_metric_voltage to TelemetryChart.Metric.VOLTAGE,
            R.id.btn_metric_rsrp to TelemetryChart.Metric.RSRP,
            R.id.btn_metric_temp to TelemetryChart.Metric.TEMPERATURE,
        )
        for ((id, metric) in metricButtonsList) {
            findViewById<Button>(id).setOnClickListener { chart.metric = metric; refresh() }
        }

        btnStart.setOnClickListener { onStartTask() }
        btnStop.setOnClickListener { onStopTask() }
        btnExport.setOnClickListener { exportToFiles() }

        renderTaskInfo()
        refresh()
    }

    /** Load the task JSON for [experimentId] from [TaskStore]. */
    private fun loadTask(): Boolean {
        if (experimentId.isEmpty()) return false
        taskJson = TaskStore.listTasks(this).firstOrNull {
            it.optString("experimentId") == experimentId
        }
        return taskJson != null
    }

    /** Render the static task header (id / run / condition / environment / phases). */
    private fun renderTaskInfo() {
        val t = taskJson ?: return
        taskTitle.text = experimentId
        val runId = t.optString("runId", "—")
        val condId = t.optString("conditionId", "—")
        val env = t.optString("environment", "AC")
        taskMeta.text = "run: $runId   cond: $condId   env: $env"
        taskPhases.text = buildPhasesSummary(t)
    }

    private fun buildPhasesSummary(task: JSONObject): String {
        val arr = task.optJSONArray("phases") ?: return ""
        val parts = mutableListOf<String>()
        for (i in 0 until arr.length()) {
            val p = arr.optJSONObject(i) ?: continue
            val name = p.optString("name", "phase$i")
            val dur = p.optDouble("durationSeconds", 0.0)
            parts += "$name ${"%.0f".format(dur)}s"
        }
        return if (parts.isNotEmpty())
            "${getString(R.string.task_card_phases_label)}: ${parts.joinToString(" → ")}"
        else ""
    }

    /** True when this task is the one currently loaded in the RunEngine. */
    private val isThisTaskActive: Boolean
        get() {
            val plan = AgentState.currentPlan ?: return false
            return plan.experimentId == experimentId
        }

    private fun onStartTask() {
        val engine = AgentState.runEngine
        // Block if another experiment is already being monitored or is armed/running.
        val otherActive =
            (AgentState.monitoringExperimentId != null && AgentState.monitoringExperimentId != experimentId) ||
            ((engine.state == RunEngine.State.ARMED || engine.state == RunEngine.State.RUNNING) &&
                AgentState.currentPlan?.experimentId != experimentId)
        if (otherActive) {
            Toast.makeText(this, R.string.task_busy_hint, Toast.LENGTH_LONG).show()
            return
        }
        val t = taskJson ?: return
        // Sanity: ensure the task JSON parses (the actual plan arrives later at
        // sync-confirm, but we validate the stored task shape up front).
        try { ExperimentPlan.fromJson(t) } catch (e: Exception) {
            Toast.makeText(this, R.string.task_load_failed, Toast.LENGTH_SHORT).show()
            return
        }
        // USB must be unplugged during experiments — live traffic (downlink
        // probing, ACK, sync-confirm) goes over the 5G air interface only, and
        // USB is reserved for post-experiment data collection. If the cable is
        // still attached at monitoring start (i.e. BEFORE the sync handshake),
        // ask the user to confirm.
        if (isUsbConnected()) {
            AlertDialog.Builder(this)
                .setTitle(R.string.usb_attached_title)
                .setMessage(R.string.usb_attached_msg)
                .setPositiveButton(R.string.usb_attached_continue) { _, _ -> enterMonitoring() }
                .setNegativeButton(R.string.usb_attached_cancel, null)
                .show()
            return
        }
        enterMonitoring()
    }

    /** Enter environment-monitoring mode (phase machine stays IDLE until the
     *  PC sync-confirm auto-arms the run). */
    private fun enterMonitoring() {
        // Environment monitoring only: sampling runs, but the phase machine stays
        // IDLE until the PC completes the sync handshake and auto-arms the run.
        startForegroundService(
            Intent(this, ExperimentService::class.java)
                .setAction(ExperimentService.ACTION_START_SERVICE)
                .putExtra(ExperimentService.EXTRA_EXPERIMENT_ID, experimentId)
        )
        AgentState.clearDisplay()
        refresh()
    }

    /** True when the phone is physically connected to a host over USB.
     *  Read from the kernel USB gadget state (no permission needed). */
    private fun isUsbConnected(): Boolean {
        return try {
            val state = java.io.File("/sys/class/android_usb/android0/state").readText().trim()
            state == "CONFIGURED" || state == "CONNECTED"
        } catch (e: Exception) {
            false
        }
    }

    private fun onStopTask() {
        startService(Intent(this, ExperimentService::class.java)
            .setAction(ExperimentService.ACTION_STOP))
        refresh()
    }

    private fun refresh() {
        val engine = AgentState.runEngine
        val state = engine.state
        val isMonitoring = AgentState.monitoringExperimentId == experimentId && state == RunEngine.State.IDLE
        val active = isThisTaskActive || isMonitoring
        val started = active && state != RunEngine.State.IDLE   // ARMED / RUNNING / COMPLETE

        // Telemetry shows as soon as the service is sampling (monitoring or run);
        // the phase section only appears once the run is actually armed.
        val showTelemetry = active
        val showPhase = started
        metricButtons.visibility = if (showTelemetry) View.VISIBLE else View.GONE
        chart.visibility = if (showTelemetry) View.VISIBLE else View.GONE
        status.visibility = if (showTelemetry) View.VISIBLE else View.GONE
        phaseSection.visibility = if (showPhase) View.VISIBLE else View.GONE

        // Action buttons depend on state.
        when {
            isMonitoring -> {
                btnStart.visibility = View.GONE
                btnStop.visibility = View.VISIBLE
                btnExport.visibility = View.GONE
                btnStart.text = getString(R.string.btn_start_task)
                taskHint.text = getString(R.string.task_monitoring_hint)
                taskHint.visibility = View.VISIBLE
            }
            !active || state == RunEngine.State.IDLE || state == RunEngine.State.ABORTED -> {
                btnStart.visibility = View.VISIBLE
                btnStop.visibility = View.GONE
                btnExport.visibility = View.GONE
                btnStart.text = getString(R.string.btn_start_task)
                taskHint.text = if (state == RunEngine.State.ABORTED && active)
                    getString(R.string.task_status_aborted)
                else getString(R.string.task_not_started_hint)
                taskHint.visibility = View.VISIBLE
            }
            state == RunEngine.State.ARMED || state == RunEngine.State.RUNNING -> {
                btnStart.visibility = View.GONE
                btnStop.visibility = View.VISIBLE
                btnExport.visibility = View.GONE
                val delay = AgentState.syncDelayMs
                taskHint.text = if (delay != null)
                    getString(R.string.task_synced_hint, delay)
                else getString(R.string.task_monitoring_hint)
                taskHint.visibility = View.VISIBLE
            }
            state == RunEngine.State.COMPLETE -> {
                btnStart.visibility = View.GONE
                btnStop.visibility = View.GONE
                btnExport.visibility = View.VISIBLE
                taskHint.text = getString(R.string.task_complete_hint)
                taskHint.visibility = View.VISIBLE
            }
        }

        // Phase progress.
        if (showPhase) {
            val nowNs = SystemClock.elapsedRealtimeNanos()
            val pp = engine.phaseProgress(nowNs)
            if (pp != null) {
                phaseLabel.text = "阶段 ${pp.index + 1}/${pp.total}: ${pp.name}"
                phaseTime.text = "已用 ${"%.0f".format(pp.elapsedSeconds)}s / ${"%.0f".format(pp.durationSeconds)}s"
                val pct = if (pp.durationSeconds > 0)
                    ((pp.elapsedSeconds / pp.durationSeconds) * 100).toInt().coerceIn(0, 100)
                else 0
                phaseProgress.progress = pct
            } else if (state == RunEngine.State.ARMED) {
                // Countdown before the first phase starts.
                val total = engine.phaseCount
                phaseLabel.text = "阶段 0/${if (total > 0) total else 1}: 准备中"
                phaseTime.text = "等待开始…"
                phaseProgress.progress = 0
            }
        }

        // Chart + status text (meaningful whenever telemetry is visible).
        if (showTelemetry) {
            chart.samples = AgentState.displaySamples
            chart.invalidate()
            val last = AgentState.displaySamples.lastOrNull()
            status.text = buildString {
                append("device: ").append(Build.MANUFACTURER).append(' ').append(Build.MODEL).append('\n')
                append("state: ").append(if (isMonitoring) "MONITORING" else state.name)
                append("  phase: ").append(engine.currentPhase ?: "—").append('\n')
                if (last != null) {
                    append(String.format("power %.1f mW | current %.0f µA | %.0f mV | RSRP %s dBm | %.1f°C",
                        (last.powerW ?: 0.0) * 1000.0,
                        last.currentUa ?: 0.0,
                        last.voltageMv ?: 0.0,
                        last.rsrpDbm?.let { "%.0f".format(it) } ?: "—",
                        last.temperatureC ?: 0.0))
                    append('\n')
                }
                AgentState.syncDelayMs?.let { append("delay: ").append("%.1f".format(it)).append(" ms\n") }
                append("experiment: ").append(experimentId)
                append("  run: ").append(AgentState.currentPlan?.runId ?: "—").append('\n')
                append("samplingHz: ").append(AgentState.samplingHz)
                append("  server: ").append(if (AgentState.serverStarted) "127.0.0.1:8420" else "off")
            }
        }
    }

    private fun requestRuntimePermissions() {
        // Only request READ_PHONE_STATE (SS-RSRP/RSRQ/SINR). ACCESS_FINE_LOCATION
        // (cell identity) is a one-time permission best granted manually.
        val perms = arrayOf(Manifest.permission.READ_PHONE_STATE)
        val missing = perms.filter { ContextCompat.checkSelfPermission(this, it) != PackageManager.PERMISSION_GRANTED }
        if (missing.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, missing.toTypedArray(), 1)
        }
    }

    private fun showHelp() {
        val help = """
            【任务详情使用帮助】

            1. 点击「开始实验」：进入环境监测，前台服务开始采样功率 / RSSI 等，但阶段机保持 IDLE，等待 PC 对时。

            2. PC 端「开始实验」启动 gNB 并持续下行；收到首个 ACK 后把 gNB 时间戳发给手机（对时），手机计算通信时延后自动 arm，依次执行 baseline → active（大流量）→ tail。

            3. 监测 / 采集期间均显示实时图表与状态；阶段进度条在对时完成后出现。

            4. 60 秒（可在「⚙」设置）无信号会自动开关飞行模式刷新，开关时间戳均记录。

            5. 结束时分别按「停止实验」；PC 与手机各自记录停止时间戳。

            6. 离线运行：开始后可拔掉 USB、熄屏，前台服务 + 唤醒锁继续采样。

            7. 完成后点击「导出数据」可导出 CSV，由 PC 经 /agent/export 拉取。
        """.trimIndent()
        AlertDialog.Builder(this)
            .setTitle("使用帮助")
            .setMessage(help)
            .setPositiveButton("关闭", null)
            .show()
    }

    /** Phone-local recovery/network settings. Experiment phase timing is
     *  intentionally controlled only by the platform run plan. */
    private fun showNoSignalSettings() {
        val prefs = getSharedPreferences("agent_settings", Context.MODE_PRIVATE)
        val currentNoSignal = prefs.getLong("no_signal_seconds", 60L)
        val currentHost = prefs.getString("server_host", "192.168.70.129") ?: "192.168.70.129"
        val currentPort = prefs.getInt("server_port", 5201)
        val currentMbps = prefs.getFloat("target_mbps", 5.0f)

        val noSignalInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(currentNoSignal.toString())
            hint = "60"
        }
        val hostInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT
            setText(currentHost)
            hint = "192.168.70.129"
        }
        val portInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER
            setText(currentPort.toString())
            hint = "5201"
        }
        val mbpsInput = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_NUMBER or InputType.TYPE_NUMBER_FLAG_DECIMAL
            setText(currentMbps.toString())
            hint = "5.0"
        }
        val probeResult = TextView(this).apply {
            text = getString(R.string.settings_server_msg)
            setTextColor(android.graphics.Color.parseColor("#666666"))
            textSize = 12f
            setPadding(0, 8, 0, 0)
        }
        val probeBtn = android.widget.Button(this).apply {
            text = getString(R.string.settings_probe_btn)
            setOnClickListener {
                val h = hostInput.text.toString().trim()
                val p = portInput.text.toString().trim().toIntOrNull() ?: 5201
                if (h.isEmpty()) {
                    probeResult.text = "请输入服务器地址"
                    return@setOnClickListener
                }
                probeResult.text = "测试中 $h:$p …"
                lifecycleScope.launch {
                    val err = withContext(Dispatchers.IO) {
                        WorkloadEngine(this@MainActivity).probeServer(h, p)
                    }
                    probeResult.text = if (err == null) {
                        "连通 ✓（TCP 握手成功，可承载满载上行）"
                    } else {
                        "不通 ✗：$err（检查服务器监听 / 5G 数据链路）"
                    }
                }
            }
        }
        val container = android.widget.LinearLayout(this).apply {
            orientation = android.widget.LinearLayout.VERTICAL
            setPadding(64, 24, 64, 0)
        }
        container.addView(TextView(this).apply {
            text = getString(R.string.settings_no_signal_title)
            setTextColor(android.graphics.Color.parseColor("#111111"))
            textSize = 14f
            setPadding(0, 0, 0, 6)
        })
        container.addView(noSignalInput)
        container.addView(TextView(this).apply {
            text = getString(R.string.settings_server_title)
            setTextColor(android.graphics.Color.parseColor("#111111"))
            textSize = 14f
            setPadding(0, 24, 0, 6)
        })
        container.addView(hostInput)
        container.addView(portInput)
        container.addView(mbpsInput)
        container.addView(probeBtn)
        container.addView(probeResult)
        container.addView(TextView(this).apply {
            text = "实验的 IDLE / LOADED 时长由平台任务统一下发。"
            setTextColor(android.graphics.Color.parseColor("#666666"))
            textSize = 12f
            setPadding(0, 12, 0, 0)
        })
        AlertDialog.Builder(this)
            .setTitle(R.string.settings_title)
            .setView(container)
            .setPositiveButton("保存") { _, _ ->
                val ns = noSignalInput.text.toString().trim().toLongOrNull()
                val host = hostInput.text.toString().trim()
                val port = portInput.text.toString().trim().toIntOrNull()
                val mbps = mbpsInput.text.toString().trim().toFloatOrNull()
                val edits = mutableListOf<String>()
                if (ns != null && ns >= 5) {
                    prefs.edit().putLong("no_signal_seconds", ns).apply()
                    edits += "无信号阈值 ${ns}s"
                }
                if (host.isNotEmpty()) {
                    prefs.edit().putString("server_host", host).apply()
                    edits += "测试站 $host"
                }
                if (port != null && port in 1..65535) {
                    prefs.edit().putInt("server_port", port).apply()
                    edits += "端口 $port"
                }
                if (mbps != null && mbps > 0f) {
                    prefs.edit().putFloat("target_mbps", mbps).apply()
                    edits += "目标 ${mbps}Mbps"
                }
                if (edits.isNotEmpty()) {
                    Toast.makeText(this, "已设置：${edits.joinToString("，")}", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(this, "请输入有效数值", Toast.LENGTH_SHORT).show()
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun exportToFiles() {
        lifecycleScope.launch {
            val dir = getExternalFilesDir(null) ?: filesDir
            val out = File(dir, "export")
            out.mkdirs()
            withContext(Dispatchers.IO) {
                val db = AppDatabase.get(this@MainActivity)
                val plan = AgentState.currentPlan
                if (plan != null) {
                    val samples = db.samples().byRun(plan.runId)
                    val markers = db.markers().byRun(plan.runId)
                    File(out, "phone_samples.csv").writeText(CsvExporter.samplesCsv(samples))
                    File(out, "phone_events.csv").writeText(CsvExporter.eventsCsv(markers))
                }
            }
            Toast.makeText(this@MainActivity,
                "exported to: ${out.absolutePath}", Toast.LENGTH_LONG).show()
        }
    }

    override fun onResume() {
        super.onResume()
        handler.post(refreshLoop)
    }

    override fun onPause() {
        super.onPause()
        // stop the UI refresh loop when the screen is off / activity hidden,
        // so the foreground service can keep sampling without UI overhead.
        handler.removeCallbacks(refreshLoop)
    }

    companion object {
        const val EXTRA_EXPERIMENT_ID = "experiment_id"
    }
}
