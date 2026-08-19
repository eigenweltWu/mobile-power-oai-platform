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
import com.xjtlu.energyagent.R
import com.xjtlu.energyagent.TaskListActivity
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.db.EventMarkerEntity
import com.xjtlu.energyagent.db.PhoneSampleEntity
import com.xjtlu.energyagent.db.SyncAnchorEntity
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
import org.json.JSONObject

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
            ACTION_START_SERVICE -> {
                // Environment monitoring only — sampling runs, but the phase
                // machine stays IDLE until the PC completes the sync handshake.
                val eid = intent?.getStringExtra(EXTRA_EXPERIMENT_ID)
                if (eid != null) AgentState.monitoringExperimentId = eid
                ensureRunning()
                recordUsbStateAtMonitoring()
            }
            ACTION_ARM -> {
                val planJson = intent.getStringExtra(EXTRA_PLAN_JSON)
                if (planJson != null) {
                    val plan = ExperimentPlan.fromJson(JSONObject(planJson))
                    arm(plan)
                }
            }
            ACTION_SYNC_CONFIRM -> handleSyncConfirm(intent)
            ACTION_REARM -> handleRearm()
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
        // The platform owns every phase and duration. A phone-local override
        // would make the measured run diverge from the recorded run plan.
        val effective = plan
        Log.i(TAG, "arm() runId=" + effective.runId +
            " idleSec=" + effective.idleSeconds +
            " collectionSec=" + effective.collectionSeconds)
        ensureRunning()
        AgentState.currentPlan = effective
        scope.launch {
            try {
                db.meta().upsertExperiment(
                    com.xjtlu.energyagent.db.ExperimentEntity(
                        effective.experimentId, effective.environment, null, null, System.currentTimeMillis()
                    )
                )
                db.meta().upsertRun(
                    com.xjtlu.energyagent.db.RunEntity(
                        effective.runId, effective.experimentId, effective.conditionId, null, null,
                        "ARMED", null, effective.startDelaySeconds, null, null
                    )
                )
                AgentState.runEngine.arm(effective, SystemClock.elapsedRealtimeNanos()) { marker ->
                    recordMarker(effective, marker)
                }
                acquireWakeLock()
                Log.i(TAG, "arm() engine ARMED phases=" + effective.phases.joinToString { "${it.name}(${it.durationSeconds}s)" })
            } catch (e: Exception) {
                Log.e(TAG, "arm() error", e)
            }
        }
    }

    /**
     * Re-trigger the phase machine on the SAME run (idle → loaded → idle) —
     * the PC sends this after a template switch restarted the gNB with new
     * RF conditions. Sampling never stops; markers record the re-trigger.
     */
    private fun handleRearm() {
        val plan = AgentState.currentPlan
        val st = AgentState.runEngine.state
        if (plan == null || (st != com.xjtlu.energyagent.run.RunEngine.State.ARMED
                        && st != com.xjtlu.energyagent.run.RunEngine.State.RUNNING)) {
            Log.i(TAG, "handleRearm ignored (state=$st plan=${plan?.runId})")
            return
        }
        Log.i(TAG, "handleRearm: re-arming runId=" + plan.runId)
        // Stop any in-flight workload — the fresh IDLE phase must be quiet.
        workload?.stop()
        recordMarker(plan, "REARM", JSONObject().apply {
            put("phaseBefore", AgentState.runEngine.currentPhase ?: "null")
            put("utcMs", System.currentTimeMillis())
        }.toString())
        arm(plan)
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

        // Traffic is owned by OAI's shared NetworkTest session. The PC follows
        // this phase over the 5G Agent channel and starts/stops the single
        // shared session, so Web UI, external clients and this Run agree.
        val wl = workload
        val networkTestType = if (AgentState.nettestRunning)
            "NETWORKTEST_${AgentState.nettestProtocol.uppercase()}_${AgentState.nettestDirection.uppercase()}"
        else null

        val charging = battery["battery_status"] as? Int
        val statusBattery = charging
        val sample = PhoneSampleEntity(
            utcEpochMs = utcMs,
            elapsedRealtimeNs = elapsedNs,
            // Monitoring-phase samples (before sync-confirm) fall back to the
            // monitoring experiment id so the whole session stays attributable
            // — and can be discarded as a unit if no run id ever arrives.
            experimentId = plan?.experimentId ?: AgentState.monitoringExperimentId,
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
            workloadType = networkTestType ?: wl?.mode?.name,
            workloadTargetMbps = if (AgentState.nettestRunning) AgentState.nettestTargetMbps else wl?.targetMbps,
            // OAI telemetry is authoritative for NetworkTest actualMbps; the
            // nc process is not charged to the app UID by Android TrafficStats.
            workloadActualMbps = if (AgentState.nettestRunning) null else wl?.actualUplinkMbps(),
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

    private fun flushBuffer(discardUnarmedEid: String? = null) {
        val batch = sampleBuffer.toList()
        sampleBuffer.clear()
        if (batch.isEmpty() && discardUnarmedEid == null) return
        scope.launch {
            try {
                if (batch.isNotEmpty()) db.samples().insertAll(batch)
                if (discardUnarmedEid != null) {
                    // The session never received a platform run id — discard
                    // the whole monitoring record (insert first, then delete,
                    // inside one coroutine so nothing races back in).
                    val s = db.samples().deleteUnarmed(discardUnarmedEid)
                    val m = db.markers().deleteUnarmed(discardUnarmedEid)
                    Log.w(TAG, "Discarded run-less record for $discardUnarmedEid (samples=$s markers=$m)")
                }
            } catch (e: Exception) {
                Log.e(TAG, "flushBuffer(discard=${discardUnarmedEid})", e)
            }
        }
    }

    private fun recordMarker(plan: ExperimentPlan?, marker: String, payloadJson: String? = null) {
        // Fall back to the monitored experiment during the pre-sync monitoring
        // window (plan is null until sync-confirm arms the engine).
        val eid = plan?.experimentId ?: AgentState.monitoringExperimentId ?: return
        val rid = plan?.runId
        val cid = plan?.conditionId
        scope.launch {
            db.markers().insert(
                EventMarkerEntity(
                    utcEpochMs = System.currentTimeMillis(),
                    elapsedRealtimeNs = SystemClock.elapsedRealtimeNanos(),
                    experimentId = eid,
                    runId = rid,
                    conditionId = cid,
                    markerType = marker,
                    payloadJson = payloadJson
                )
            )
        }
    }

    /**
     * Handle the PC sync-confirm handshake: the PC received the first uplink ACK,
     * recorded the gNB data timestamp, and now tells the phone the two clocks so
     * the phone can compute the communication delay and auto-arm the run.
     *
     * Idempotent: a duplicate confirm while already armed just records a marker.
     */
    private fun handleSyncConfirm(intent: Intent) {
        val pcTs = intent.getLongExtra(EXTRA_PC_TS, 0L)
        val gnbTs = intent.getLongExtra(EXTRA_GNB_TS, 0L)
        val phoneTs = intent.getLongExtra(EXTRA_PHONE_TS, System.currentTimeMillis())
        val phoneElapsed = intent.getLongExtra(EXTRA_PHONE_ELAPSED, SystemClock.elapsedRealtimeNanos())
        val delay = intent.getDoubleExtra(EXTRA_DELAY, (phoneTs - pcTs).toDouble())
        val planJson = intent.getStringExtra(EXTRA_PLAN_JSON)

        ensureRunning()

        // Double idempotency: if the engine already left IDLE (a prior confirm
        // armed it), just record a duplicate marker and return.
        if (AgentState.runEngine.state != com.xjtlu.energyagent.run.RunEngine.State.IDLE) {
            val dup = payloadOf(pcTs, gnbTs, phoneTs, phoneElapsed, delay)
            recordMarker(AgentState.currentPlan, "SYNC_CONFIRM_DUP", dup.toString())
            Log.i(TAG, "handleSyncConfirm dup (state=${AgentState.runEngine.state})")
            return
        }

        val plan = if (planJson != null) ExperimentPlan.fromJson(JSONObject(planJson)) else AgentState.currentPlan
        if (plan == null) {
            Log.e(TAG, "handleSyncConfirm: no plan available, ignoring")
            return
        }
        AgentState.currentPlan = plan
        AgentState.syncDelayMs = delay
        AgentState.monitoringExperimentId = plan.experimentId

        val payload = payloadOf(pcTs, gnbTs, phoneTs, phoneElapsed, delay)
        scope.launch {
            try {
                db.sync().insert(
                    SyncAnchorEntity(
                        direction = "before", attemptIndex = 0,
                        t1Ms = pcTs, t2UtcMs = phoneTs, t2ElapsedNs = phoneElapsed, t3Ms = null
                    )
                )
                db.markers().insert(
                    EventMarkerEntity(
                        utcEpochMs = System.currentTimeMillis(),
                        elapsedRealtimeNs = SystemClock.elapsedRealtimeNanos(),
                        experimentId = plan.experimentId,
                        runId = plan.runId,
                        conditionId = plan.conditionId,
                        markerType = "SYNC_CONFIRM",
                        payloadJson = payload.toString()
                    )
                )
                Log.i(TAG, "handleSyncConfirm: anchor+marker stored delay=${delay}ms")
            } catch (e: Exception) {
                Log.e(TAG, "handleSyncConfirm db error", e)
            }
        }
        Log.i(TAG, "handleSyncConfirm: auto-arming runId=${plan.runId} delay=${delay}ms")
        arm(plan)
    }

    private fun payloadOf(pcTs: Long, gnbTs: Long, phoneTs: Long, phoneElapsed: Long, delay: Double): JSONObject =
        JSONObject().apply {
            put("pc_ts_ms", pcTs)
            put("gnb_data_timestamp_ms", gnbTs)
            put("phone_ts_ms", phoneTs)
            put("phone_elapsed_ns", phoneElapsed)
            put("delay_ms", delay)
        }

    /**
     * Record a DOWNLINK_ACK marker for every downlink ping the PC sends, so the
     * timeline shows the ACK/train even before sync-confirm arms the run. Uses
     * the current plan, falling back to the monitored experiment id.
     */
    fun recordDownlinkAck(seq: Int, pcSendMs: Long, phoneRecvMs: Long, phoneSendMs: Long, phoneElapsedNs: Long) {
        val plan = AgentState.currentPlan
        val eid = plan?.experimentId ?: AgentState.monitoringExperimentId ?: return
        val payload = JSONObject().apply {
            put("seq", seq)
            put("pc_send_ms", pcSendMs)
            put("phone_recv_ms", phoneRecvMs)
            put("phone_send_ms", phoneSendMs)
            put("phone_elapsed_ns", phoneElapsedNs)
        }
        scope.launch {
            try {
                db.markers().insert(
                    EventMarkerEntity(
                        utcEpochMs = phoneRecvMs,
                        elapsedRealtimeNs = phoneElapsedNs,
                        experimentId = eid,
                        runId = plan?.runId,
                        conditionId = plan?.conditionId,
                        markerType = "DOWNLINK_ACK",
                        payloadJson = payload.toString()
                    )
                )
            } catch (e: Exception) {
                Log.e(TAG, "recordDownlinkAck error", e)
            }
        }
    }

    private fun noSignalSeconds(): Long {
        val prefs = getSharedPreferences("agent_settings", Context.MODE_PRIVATE)
        return prefs.getLong("no_signal_seconds", 60L)
    }

    /** Record the USB connection state when monitoring starts (before the sync
     *  handshake). Experiments communicate over the 5G air interface only; a
     *  cable still attached at this point is an anomaly worth keeping in the
     *  data trail (the UI also asks the user to confirm before monitoring). */
    private fun recordUsbStateAtMonitoring() {
        val connected = try {
            val state = java.io.File("/sys/class/android_usb/android0/state").readText().trim()
            state == "CONFIGURED" || state == "CONNECTED"
        } catch (e: Exception) { false }
        recordMarker(
            null,
            if (connected) "USB_CONNECTED_AT_MONITORING" else "MONITORING_STARTED",
            org.json.JSONObject().put("usbConnected", connected).toString()
        )
        if (connected) {
            Log.w(TAG, "USB still connected when monitoring started — user should unplug it")
        }
    }

    private fun monitorSignal(nr: Map<String, Any?>, nowNs: Long) {
        // The PC deliberately cycles airplane mode while restarting the gNB.
        // Forget the pre-restart signal timestamp during that external cycle;
        // otherwise the stale timeout immediately triggers a second cycle as
        // soon as Android resumes sampling, tearing down a healthy new attach.
        val airplaneOn = android.provider.Settings.Global.getInt(
            contentResolver, android.provider.Settings.Global.AIRPLANE_MODE_ON, 0) != 0
        if (airplaneOn) {
            lastSignalElapsedNs = 0L
            return
        }
        val hasSignal = (nr["ss_rsrp_dbm"] != null) || (nr["network_type"] != null && nr["network_type"] != "OTHER")
        if (hasSignal) {
            lastSignalElapsedNs = nowNs
            return
        }
        if (airplaneToggling) return
        // Start a fresh grace period after service/app start or airplane-off.
        // Previously zero meant "never recover" when the service began without
        // signal, while a stale non-zero value could recover far too early.
        if (lastSignalElapsedNs == 0L) {
            lastSignalElapsedNs = nowNs
            return
        }
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
        // Record the phone-side stop timestamp before tearing down, so both the
        // PC stop and the phone stop anchors exist for timeline alignment.
        val plan = AgentState.currentPlan
        // If the session NEVER received a run id from the platform (no
        // sync-confirm ever armed the engine), discard the whole record —
        // run-less monitoring data cannot be matched on the PC side anyway.
        val neverArmed = plan?.runId.isNullOrBlank()
        val discardEid = if (neverArmed) (plan?.experimentId ?: AgentState.monitoringExperimentId) else null
        if (!neverArmed && (plan != null || AgentState.monitoringExperimentId != null)) {
            val payload = JSONObject().apply {
                put("stop_utc_ms", System.currentTimeMillis())
                put("stop_elapsed_ns", SystemClock.elapsedRealtimeNanos())
            }
            recordMarker(plan, "PHONE_STOP", payload.toString())
        }
        AgentState.runEngine.abort { recordMarker(AgentState.currentPlan, it) }
        workload?.stop()
        flushBuffer(discardEid)
        wakeLock?.let { if (it.isHeld) it.release() }
        AgentState.currentPlan = null
        AgentState.monitoringExperimentId = null
        AgentState.syncDelayMs = null
        // Back to a fresh IDLE so the NEXT experiment can enter monitoring and
        // arm via sync-confirm again (ABORTED would block both — the run is
        // already fully torn down and its markers recorded at this point).
        AgentState.runEngine.reset()
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
            this, 0, Intent(this, TaskListActivity::class.java),
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
        const val ACTION_SYNC_CONFIRM = "com.xjtlu.energyagent.SYNC_CONFIRM"
        const val ACTION_REARM = "com.xjtlu.energyagent.REARM"
        const val ACTION_STOP = "com.xjtlu.energyagent.STOP"
        const val EXTRA_PLAN_JSON = "plan_json"
        const val EXTRA_EXPERIMENT_ID = "experiment_id"
        const val EXTRA_PC_TS = "pc_ts_ms"
        const val EXTRA_GNB_TS = "gnb_ts_ms"
        const val EXTRA_PHONE_TS = "phone_ts_ms"
        const val EXTRA_PHONE_ELAPSED = "phone_elapsed_ns"
        const val EXTRA_DELAY = "delay_ms"
    }
}
