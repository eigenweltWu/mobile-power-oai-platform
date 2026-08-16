package com.xjtlu.energyagent

import android.Manifest
import android.content.Intent
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.export.CsvExporter
import com.xjtlu.energyagent.service.ExperimentService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.io.File

/** Minimal UI — live chart + status + manual start/stop/export. The PC drives the run. */
class MainActivity : AppCompatActivity() {

    private lateinit var status: TextView
    private lateinit var chart: TelemetryChart
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
        status = findViewById(R.id.status)
        chart = findViewById(R.id.chart)

        requestRuntimePermissions()

        findViewById<Button>(R.id.btn_help).setOnClickListener { showHelp() }

        val metricButtons = listOf(
            R.id.btn_metric_power to TelemetryChart.Metric.POWER,
            R.id.btn_metric_current to TelemetryChart.Metric.CURRENT,
            R.id.btn_metric_voltage to TelemetryChart.Metric.VOLTAGE,
            R.id.btn_metric_rsrp to TelemetryChart.Metric.RSRP,
            R.id.btn_metric_temp to TelemetryChart.Metric.TEMPERATURE,
        )
        for ((id, metric) in metricButtons) {
            findViewById<Button>(id).setOnClickListener { chart.metric = metric; refresh() }
        }

        findViewById<Button>(R.id.btn_start).setOnClickListener {
            startForegroundService(Intent(this, ExperimentService::class.java)
                .setAction(ExperimentService.ACTION_START_SERVICE))
            refresh()
        }
        findViewById<Button>(R.id.btn_stop).setOnClickListener {
            startService(Intent(this, ExperimentService::class.java).setAction(ExperimentService.ACTION_STOP))
            refresh()
        }
        findViewById<Button>(R.id.btn_export).setOnClickListener { exportToFiles() }

        startForegroundService(Intent(this, ExperimentService::class.java)
            .setAction(ExperimentService.ACTION_START_SERVICE))
        refresh()
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

    private fun refresh() {
        chart.samples = AgentState.displaySamples
        chart.invalidate()
        val plan = AgentState.currentPlan
        val engine = AgentState.runEngine
        val last = AgentState.displaySamples.lastOrNull()
        status.text = buildString {
            append("device: ").append(Build.MANUFACTURER).append(' ').append(Build.MODEL).append('\n')
            append("state: ").append(engine.state.name)
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
            append("experiment: ").append(plan?.experimentId ?: "—")
            append("  run: ").append(plan?.runId ?: "—").append('\n')
            append("samplingHz: ").append(AgentState.samplingHz)
            append("  server: ").append(if (AgentState.serverStarted) "127.0.0.1:8420" else "off")
        }
    }

    private fun showHelp() {
        val help = """
            【5G Energy Experiment Agent 使用帮助】

            1. 实时图表：显示最近 1 分钟的遥测，可切换 功率/电流/电压/SS-RSRP/温度。缺失值不画（绝不填 0）。

            2. 正式实验流程（离线）：
               PC 通过 USB 下发实验 plan → 点 ARM → 拔掉 USB → 熄屏 → App 用 monotonic 时钟倒计时执行 baseline/active/tail → 完成后重连 USB → PC 拉取数据。

            3. 后台运行：前台服务 + 部分唤醒锁在熄屏下继续采样；不保持屏幕常亮，屏幕不引入额外功耗。

            4. 采集内容：电池（CURRENT_NOW/AVERAGE/CHARGE_COUNTER，保留 OEM 符号）、电压、SOC、温度；NR（SS-RSRP/RSRQ/SINR）；thermal；confounder（屏幕/充电/Wi-Fi/蓝牙/飞行模式）。

            5. 权限：READ_PHONE_STATE 用于信号强度；小区身份（PCI/NCI/TAC）需手动授予 ACCESS_FINE_LOCATION。

            6. 数据：本地 Room 存储，实验后由 PC 经 /agent/export 拉取（USB-only 通道），不经过 Wi-Fi/云端。
        """.trimIndent()
        AlertDialog.Builder(this)
            .setTitle("使用帮助")
            .setMessage(help)
            .setPositiveButton("关闭", null)
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
            status.text = "exported to:\n${out.absolutePath}"
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
}
