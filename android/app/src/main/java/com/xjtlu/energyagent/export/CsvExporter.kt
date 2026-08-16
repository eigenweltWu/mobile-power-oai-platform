package com.xjtlu.energyagent.export

import com.xjtlu.energyagent.db.EventMarkerEntity
import com.xjtlu.energyagent.db.PhoneSampleEntity
import com.xjtlu.energyagent.db.SyncAnchorEntity
import org.json.JSONArray
import org.json.JSONObject

/**
 * Builds the export payload strings exactly per DATA_SCHEMA §2.
 * Missing values are emitted as empty fields (null) — never 0.
 */
object CsvExporter {

    private val SAMPLE_HEADERS = listOf(
        "utc_epoch_ms", "elapsed_realtime_ns",
        "experiment_id", "run_id", "condition_id", "session_id", "device_id", "phase",
        "battery_current_now_ua", "battery_current_average_ua", "battery_voltage_mv", "battery_power_w",
        "charge_counter_uah", "soc_percent", "battery_temperature_c", "thermal_status", "thermal_headroom",
        "ss_rsrp_dbm", "ss_rsrq_db", "ss_sinr_db",
        "csi_rsrp_dbm", "csi_rsrq_db", "csi_sinr_db", "csi_cqi",
        "nrarfcn", "pci", "nci", "tac", "network_type",
        "screen_state", "plugged", "charging", "wifi_state", "bluetooth_state", "airplane_mode",
        "workload_type", "workload_target_mbps", "workload_actual_mbps", "app_tx_bytes", "app_rx_bytes",
        "sample_quality_flags"
    )

    private val EVENT_HEADERS = listOf(
        "utc_epoch_ms", "elapsed_realtime_ns", "experiment_id", "run_id", "condition_id", "marker_type", "payload_json"
    )

    fun samplesCsv(samples: List<PhoneSampleEntity>): String {
        val sb = StringBuilder()
        sb.append(SAMPLE_HEADERS.joinToString(",")).append('\n')
        for (s in samples) {
            sb.append(listOf(
                s.utcEpochMs, s.elapsedRealtimeNs, s.experimentId, s.runId, s.conditionId, s.sessionId, s.deviceId, s.phase,
                s.batteryCurrentNowUa, s.batteryCurrentAverageUa, s.batteryVoltageMv, s.batteryPowerW,
                s.chargeCounterUah, s.socPercent, s.batteryTemperatureC, s.thermalStatus, s.thermalHeadroom,
                s.ssRsrpDbm, s.ssRsrqDb, s.ssSinrDb,
                s.csiRsrpDbm, s.csiRsrqDb, s.csiSinrDb, s.csiCqi,
                s.nrarfcn, s.pci, s.nci, s.tac, s.networkType,
                s.screenState, s.plugged, s.charging, s.wifiState, s.bluetoothState, s.airplaneMode,
                s.workloadType, s.workloadTargetMbps, s.workloadActualMbps, s.appTxBytes, s.appRxBytes,
                s.sampleQualityFlags
            ).joinToString(",") { csvCell(it) }).append('\n')
        }
        return sb.toString()
    }

    fun eventsCsv(markers: List<EventMarkerEntity>): String {
        val sb = StringBuilder()
        sb.append(EVENT_HEADERS.joinToString(",")).append('\n')
        for (m in markers) {
            sb.append(listOf(
                m.utcEpochMs, m.elapsedRealtimeNs, m.experimentId, m.runId, m.conditionId, m.markerType,
                m.payloadJson?.let { "\"" + it.replace("\"", "\"\"") + "\"" } ?: ""
            ).joinToString(",") { it?.toString() ?: "" }).append('\n')
        }
        return sb.toString()
    }

    fun syncJson(anchors: List<SyncAnchorEntity>): String {
        val arr = JSONArray()
        for (a in anchors) {
            arr.put(JSONObject().apply {
                put("attempt_index", a.attemptIndex)
                put("t1_ms", a.t1Ms ?: JSONObject.NULL)
                put("t2_utc_ms", a.t2UtcMs ?: JSONObject.NULL)
                put("t2_elapsed_ns", a.t2ElapsedNs ?: JSONObject.NULL)
                put("t3_ms", a.t3Ms ?: JSONObject.NULL)
            })
        }
        return JSONObject().apply { put("anchors", arr) }.toString(2)
    }

    private fun csvCell(v: Any?): String = when (v) {
        null -> ""
        is Double -> if (v.isNaN() || v.isInfinite()) "" else trim(v)
        is Float -> if (v.isNaN() || v.isInfinite()) "" else trim(v.toDouble())
        is String -> "\"" + v.replace("\"", "\"\"") + "\""
        else -> v.toString()
    }

    private fun trim(v: Double): String =
        if (v == v.toLong().toDouble()) v.toLong().toString() else v.toString()
}
