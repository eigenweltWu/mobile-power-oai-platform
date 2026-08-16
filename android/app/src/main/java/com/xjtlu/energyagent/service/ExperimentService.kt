package com.xjtlu.energyagent.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import androidx.core.app.NotificationCompat
import com.xjtlu.energyagent.AgentState
import com.xjtlu.energyagent.MainActivity
import com.xjtlu.energyagent.R
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.db.EventMarkerEntity
import com.xjtlu.energyagent.db.PhoneSampleEntity
import com.xjtlu.energyagent.run.ExperimentPlan
import com.xjtlu.energyagent.run.WorkloadEngine
import com.xjtlu.energyagent.telemetry.TelemetryCollector
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Foreground service that runs the offline experiment: samples telemetry at a
 * configurable rate, advances the run engine by elapsedRealtimeNanos, and stores
 * everything locally. Continues with the screen OFF (foreground + wake lock).
 */
class ExperimentService : Service() {

    private lateinit var db: AppDatabase
    private var telemetry: TelemetryCollector? = null
    private var workload: WorkloadEngine? = null
    private var samplingJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    private var sampleBuffer = mutableListOf<PhoneSampleEntity>()

    // airplane-mode refresh: if no NR signal for noSignalSeconds, toggle for 3 s
    private var lastSignalElapsedNs = 0L
    @Volatile private var airplaneToggling = false

    override fun onCreate() {
        super.onCreate()
        db = AppDatabase.get(this)
        startForeground(1, buildNotification())
        AgentState.service = this
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        Log.i(TAG, "onStartCommand action=" + intent?.action)
        when (intent?.action) {
            ACTION_START_SERVICE -> ensureRunning()
            ACTION_ARM -> {
                val planJson = intent.getStringExtra(EXTRA_PLAN_JSON)
                if (planJson != null) {
                    val plan = ExperimentPlan.fromJson(org.json.JSONObject(planJson))
                    arm(plan)
                }
            }
            ACTION_STOP -> stopExperiment()
        }
        return START_STICKY
    }

    private fun ensureRunning() {
        if (telemetry == null) { telemetry = TelemetryCollector(this); Log.i(TAG, "telemetry created") }
        if (workload == null) {
            workload = WorkloadEngine(this)
            AgentState.workload = workload
        }
        startSamplingIfNeeded()
    }

    private fun arm(plan: ExperimentPlan) {
        Log.i(TAG, "arm() runId=" + plan.runId)
        ensureRunning()
        AgentState.currentPlan = plan
        scope.launch {
            try {
                db.meta().upsertExperiment(
                    com.xjtlu.energyagent.db.ExperimentEntity(
                        plan.experimentId, plan.environment, null, null, System.currentTimeMillis()
                    )
                )
                db.meta().upsertRun(
                    com.xjtlu.energyagent.db.RunEntity(
                        plan.runId, plan.experimentId, plan.conditionId, null, null,
                        "ARMED", null, plan.startDelaySeconds, null, null
                    )
                )
                AgentState.runEngine.arm(plan, SystemClock.elapsedRealtimeNanos()) { marker ->
                    recordMarker(plan, marker)
                }
                acquireWakeLock()
                Log.i(TAG, "arm() engine ARMED")
            } catch (e: Exception) {
                Log.e(TAG, "arm() error", e)
            }
        }
    }

    private fun startSamplingIfNeeded() {
        if (samplingJob?.isActive == true) return
        Log.i(TAG, "starting sampling loop @ " + AgentState.samplingHz + " Hz")
        samplingJob = scope.launch {
            var n = 0
            while (isActive) {
                try {
                    tick()
                    if (++n % 25 == 0) Log.i(TAG, "sampled n=" + n)
                } catch (e: Exception) {
                    Log.e(TAG, "tick error", e)
                }
                delay((1000L / AgentState.samplingHz).coerceAtLeast(100))
            }
            Log.i(TAG, "sampling loop exited")
        }
    }

    private fun tick() {
        val t = telemetry ?: return
        val plan = AgentState.currentPlan
        val elapsedNs = SystemClock.elapsedRealtimeNanos()
        val utcMs = System.currentTimeMillis()

        val battery = t.battery()
        val nr = t.nr()
        val thermal = t.thermal()
        val confounders = t.confounders()

        monitorSignal(nr, elapsedNs)

        val phase = AgentState.runEngine.update(elapsedNs) { marker -> recordMarker(plan, marker) }

        // workload control: start on ACTIVE, stop otherwise (for UL modes in the plan)
        val wl = workload
        if (wl != null && AgentState.runEngine.state == com.xjtlu.energyagent.run.RunEngine.State.RUNNING) {
            if (phase == "ACTIVE" && wl.mode == WorkloadEngine.Mode.IDLE) {
                // start UL_CBR if the condition indicates traffic; server address is configurable
                startWorkloadIfNeeded()
            } else if (phase != "ACTIVE" && wl.mode != WorkloadEngine.Mode.IDLE) {
                wl.stop()
            }
        }

        val charging = battery["battery_status"] as? Int
        val statusBattery = charging
        val sample = PhoneSampleEntity(
            utcEpochMs = utcMs,
            elapsedRealtimeNs = elapsedNs,
            experimentId = plan?.experimentId,
            runId = plan?.runId,
            conditionId = plan?.conditionId,
            sessionId = plan?.runId,
            deviceId = null,
            phase = phase,
            batteryCurrentNowUa = battery["battery_current_now_ua"] as? Double,
            batteryCurrentAverageUa = battery["battery_current_average_ua"] as? Double,
            batteryVoltageMv = battery["battery_voltage_mv"] as? Double,
            batteryPowerW = battery["battery_power_w"] as? Double,
            chargeCounterUah = battery["charge_counter_uah"] as? Double,
            socPercent = battery["soc_percent"] as? Double,
            batteryTemperatureC = battery["battery_temperature_c"] as? Double,
            thermalStatus = thermal["thermal_status"] as? Int,
            thermalHeadroom = thermal["thermal_headroom"] as? Double,
            ssRsrpDbm = nr["ss_rsrp_dbm"] as? Double,
            ssRsrqDb = nr["ss_rsrq_db"] as? Double,
            ssSinrDb = nr["ss_sinr_db"] as? Double,
            csiRsrpDbm = nr["csi_rsrp_dbm"] as? Double,
            csiRsrqDb = nr["csi_rsrq_db"] as? Double,
            csiSinrDb = nr["csi_sinr_db"] as? Double,
            csiCqi = nr["csi_cqi"] as? Int,
            nrarfcn = nr["nrarfcn"] as? Int,
            pci = nr["pci"] as? Int,
            nci = nr["nci"] as? String,
            tac = nr["tac"] as? Int,
            networkType = nr["network_type"] as? String,
            screenState = confounders["screen_state"] as? String,
            plugged = battery["plugged"] as? Int,
            charging = if (statusBattery == android.os.BatteryManager.BATTERY_STATUS_CHARGING) 1 else 0,
            wifiState = confounders["wifi_state"] as? String,
            bluetoothState = confounders["bluetooth_state"] as? String,
            airplaneMode = confounders["airplane_mode"] as? Int,
            workloadType = wl?.mode?.name,
            workloadTargetMbps = wl?.targetMbps,
            workloadActualMbps = wl?.actualUplinkMbps(),
            appTxBytes = wl?.appUidTxBytes,
            appRxBytes = wl?.appUidRxBytes,
            sampleQualityFlags = null,
        )
        sampleBuffer.add(sample)
        if (sampleBuffer.size >= AgentState.samplingHz * 2) flushBuffer()

        // on-screen rolling chart (last ~1 min)
        AgentState.recordDisplay(
            com.xjtlu.energyagent.DisplaySample(
                elapsedNs, sample.batteryCurrentNowUa, sample.batteryVoltageMv,
                sample.batteryPowerW, sample.ssRsrpDbm, sample.batteryTemperatureC
            )
        )
    }

    private fun flushBuffer() {
        if (sampleBuffer.isEmpty()) return
        val batch = sampleBuffer.toList()
        sampleBuffer.clear()
        scope.launch { db.samples().insertAll(batch) }
    }

    private fun recordMarker(plan: ExperimentPlan?, marker: String) {
        if (plan == null) return
        scope.launch {
            db.markers().insert(
                EventMarkerEntity(
                    utcEpochMs = System.currentTimeMillis(),
                    elapsedRealtimeNs = SystemClock.elapsedRealtimeNanos(),
                    experimentId = plan.experimentId,
                    runId = plan.runId,
                    conditionId = plan.conditionId,
                    markerType = marker,
                    payloadJson = null
                )
            )
        }
    }

    private fun startWorkloadIfNeeded() {
        // Server address is provided by the plan/session (default: OAI external DN host).
        // The app never knows gNB internals; the server is just an external-DN sink.
        workload?.start(WorkloadEngine.Mode.UL_CBR, 5.0, "192.168.70.129", 5201)
    }

    private fun noSignalSeconds(): Long {
        val prefs = getSharedPreferences("agent_settings", Context.MODE_PRIVATE)
        return prefs.getLong("no_signal_seconds", 60L)
    }

    private fun monitorSignal(nr: Map<String, Any?>, nowNs: Long) {
        val hasSignal = (nr["ss_rsrp_dbm"] != null) || (nr["network_type"] != null && nr["network_type"] != "OTHER")
        if (hasSignal) {
            lastSignalElapsedNs = nowNs
            return
        }
        if (airplaneToggling || lastSignalElapsedNs == 0L) return
        val thresholdNs = noSignalSeconds() * 1_000_000_000L
        if (nowNs - lastSignalElapsedNs >= thresholdNs) {
            airplaneToggling = true
            recordMarker(AgentState.currentPlan, "AIRPLANE_MODE_ON")
            toggleAirplaneMode(true)
            scope.launch {
                delay(3000)
                recordMarker(AgentState.currentPlan, "AIRPLANE_MODE_OFF")
                toggleAirplaneMode(false)
                lastSignalElapsedNs = SystemClock.elapsedRealtimeNanos()
                airplaneToggling = false
            }
        }
    }

    private fun toggleAirplaneMode(on: Boolean) {
        try {
            android.provider.Settings.Global.putInt(
                contentResolver, android.provider.Settings.Global.AIRPLANE_MODE_ON, if (on) 1 else 0)
            val intent = Intent(Intent.ACTION_AIRPLANE_MODE_CHANGED)
            intent.putExtra("state", on)
            sendBroadcast(intent)
            Log.i(TAG, "airplane mode ${if (on) "ON" else "OFF"}")
        } catch (e: Exception) {
            Log.e(TAG, "airplane toggle failed (needs WRITE_SECURE_SETTINGS)", e)
        }
    }

    private fun acquireWakeLock() {
        if (wakeLock == null) {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "energyagent:run")
        }
        wakeLock?.acquire(12 * 60 * 60 * 1000L)
    }

    fun stopExperiment() {
        AgentState.runEngine.abort { recordMarker(AgentState.currentPlan, it) }
        workload?.stop()
        flushBuffer()
        wakeLock?.let { if (it.isHeld) it.release() }
        AgentState.currentPlan = null
    }

    private fun buildNotification(): Notification {
        val channelId = "energyagent"
        if (Build.VERSION.SDK_INT >= 26) {
            val nm = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
            nm.createNotificationChannel(
                NotificationChannel(channelId, "Experiment Agent", NotificationManager.IMPORTANCE_LOW)
            )
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, channelId)
            .setContentTitle("5G Energy Experiment Agent")
            .setContentText("Offline experiment recording")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
    }

    override fun onDestroy() {
        stopExperiment()
        telemetry?.release()
        scope.cancel()
        AgentState.service = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    companion object {
        const val TAG = "EnergyAgent"
        const val ACTION_START_SERVICE = "com.xjtlu.energyagent.START"
        const val ACTION_ARM = "com.xjtlu.energyagent.ARM"
        const val ACTION_STOP = "com.xjtlu.energyagent.STOP"
        const val EXTRA_PLAN_JSON = "plan_json"
    }
}
