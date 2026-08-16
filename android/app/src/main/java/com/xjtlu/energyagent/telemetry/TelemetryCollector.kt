package com.xjtlu.energyagent.telemetry

import android.bluetooth.BluetoothAdapter
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.net.wifi.WifiManager
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.HandlerThread
import android.os.Looper
import android.os.PowerManager
import android.provider.Settings
import android.telephony.CellIdentityNr
import android.telephony.CellSignalStrengthNr
import android.telephony.PhoneStateListener
import android.telephony.SignalStrength
import android.telephony.TelephonyManager
import android.telephony.TelephonyManager.PHONE_TYPE_NONE

/**
 * Reads battery, NR public telemetry, thermal state and confounders.
 * Unsupported values are null (never 0). No gNB-privileged features here
 * (no TPC/PH/PRB/MCS/HARQ — those come only from OAI).
 */
class TelemetryCollector(context: Context) {

    private val appContext = context.applicationContext
    private val telephony: TelephonyManager =
        appContext.getSystemService(Context.TELEPHONY_SERVICE) as TelephonyManager
    private val power: PowerManager =
        appContext.getSystemService(Context.POWER_SERVICE) as PowerManager

    private val handlerThread = HandlerThread("nr-signal").apply { start() }

    @Volatile private var nrSignal: CellSignalStrengthNr? = null
    @Volatile private var nrIdentity: CellIdentityNr? = null
    @Volatile private var networkType: String? = null
    @Volatile private var thermalStatus: Int = PowerManager.THERMAL_STATUS_NONE

    @Suppress("DEPRECATION")
    private val phoneStateListener = object : PhoneStateListener() {
        @Deprecated("Deprecated in Java")
        override fun onSignalStrengthsChanged(signalStrength: SignalStrength) {
            val cells = signalStrength.cellSignalStrengths
            for (c in cells) {
                if (c is CellSignalStrengthNr) nrSignal = c
            }
        }

        @Deprecated("Deprecated in Java")
        override fun onCellInfoChanged(cellInfo: List<android.telephony.CellInfo>) {
            for (ci in cellInfo) {
                if (ci is android.telephony.CellInfoNr) {
                    val id = ci.cellIdentity as? CellIdentityNr
                    if (id != null) nrIdentity = id
                }
            }
        }

        override fun onDataConnectionStateChanged(state: Int, networkType: Int) {
            this@TelemetryCollector.networkType = mapNetworkType(networkType)
        }
    }

    private val thermalListener = PowerManager.OnThermalStatusChangedListener { status ->
        thermalStatus = status
    }

    init {
        // Signal strength (SS-RSRP/RSRQ/SINR) needs READ_PHONE_STATE only.
        // Cell identity (LISTEN_CELL_INFO) additionally needs ACCESS_FINE_LOCATION,
        // a one-time permission; it is not requested here so that a missing
        // FINE_LOCATION cannot disable signal strength. Cell identity (PCI/NCI/TAC)
        // stays null unless granted manually.
        try {
            telephony.listen(phoneStateListener,
                PhoneStateListener.LISTEN_SIGNAL_STRENGTHS or
                PhoneStateListener.LISTEN_DATA_CONNECTION_STATE)
        } catch (_: Exception) {
            // no telephony permission — NR fields stay null
        }
        try {
            power.addThermalStatusListener(thermalListener)
        } catch (_: Exception) {
        }
    }

    fun release() {
        try { telephony.listen(phoneStateListener, PhoneStateListener.LISTEN_NONE) } catch (_: Exception) {}
        try { power.removeThermalStatusListener(thermalListener) } catch (_: Exception) {}
        handlerThread.quitSafely()
    }

    /* ---- battery --------------------------------------------------------- */

    fun battery(): Map<String, Any?> {
        val bm = appContext.getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val intent = appContext.registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))

        fun prop(id: Int): Double? = try {
            val v = bm.getIntProperty(id)
            if (v == Int.MIN_VALUE) null else v.toDouble()
        } catch (_: Exception) { null }

        val currentNow = prop(BatteryManager.BATTERY_PROPERTY_CURRENT_NOW)
        val currentAvg = prop(BatteryManager.BATTERY_PROPERTY_CURRENT_AVERAGE)
        val chargeCounter = prop(BatteryManager.BATTERY_PROPERTY_CHARGE_COUNTER)
        val voltageMv = intent?.getIntExtra(BatteryManager.EXTRA_VOLTAGE, -1)?.let { if (it < 0) null else it.toDouble() }
        val tempTenths = intent?.getIntExtra(BatteryManager.EXTRA_TEMPERATURE, -1)
        val temperatureC = tempTenths?.let { if (it < 0) null else it / 10.0 }
        val level = intent?.getIntExtra(BatteryManager.EXTRA_LEVEL, -1)
        val scale = intent?.getIntExtra(BatteryManager.EXTRA_SCALE, -1)
        val soc = if (level != null && scale != null && scale > 0) level * 100.0 / scale else null

        val powerW = if (currentNow != null && voltageMv != null) {
            // |I(uA)| * V(mV) -> W : 1e-6 * 1e-3 = 1e-9
            kotlin.math.abs(currentNow) * voltageMv / 1e9
        } else null

        return mapOf(
            "battery_current_now_ua" to currentNow,
            "battery_current_average_ua" to currentAvg,
            "battery_voltage_mv" to voltageMv,
            "battery_power_w" to powerW,
            "charge_counter_uah" to chargeCounter,
            "soc_percent" to soc,
            "battery_temperature_c" to temperatureC,
            "battery_status" to intent?.getIntExtra(BatteryManager.EXTRA_STATUS, -1),
            "battery_health" to intent?.getIntExtra(BatteryManager.EXTRA_HEALTH, -1),
            "plugged" to (intent?.getIntExtra(BatteryManager.EXTRA_PLUGGED, 0) ?: 0),
        )
    }

    /* ---- NR -------------------------------------------------------------- */

    fun nr(): Map<String, Any?> {
        val s = nrSignal
        val id = nrIdentity
        // CellSignalStrengthNr returns Integer.MAX_VALUE for unavailable values;
        // map those to null (missing != 0, and 2.147e9 is not a valid dBm/dB).
        fun validDb(v: Int?): Double? = v?.takeIf { it != Int.MAX_VALUE }?.toDouble()
        return mapOf(
            "ss_rsrp_dbm" to validDb(s?.ssRsrp),
            "ss_rsrq_db" to validDb(s?.ssRsrq),
            "ss_sinr_db" to validDb(s?.ssSinr),
            "csi_rsrp_dbm" to (if (Build.VERSION.SDK_INT >= 31) validDb(s?.csiRsrp) else null),
            "csi_rsrq_db" to (if (Build.VERSION.SDK_INT >= 31) validDb(s?.csiRsrq) else null),
            "csi_sinr_db" to (if (Build.VERSION.SDK_INT >= 31) validDb(s?.csiSinr) else null),
            "csi_cqi" to null,  // not exposed reliably on public API for this device
            "nrarfcn" to id?.nrarfcn,
            "pci" to id?.pci,
            "nci" to (id?.nci?.toString()?.takeIf { Build.VERSION.SDK_INT >= 29 }),
            "tac" to id?.tac,
            "network_type" to (networkType ?: mapNetworkType(telephony.dataNetworkType)),
        )
    }

    /* ---- thermal --------------------------------------------------------- */

    fun thermal(): Map<String, Any?> {
        var headroom: Double? = null
        if (Build.VERSION.SDK_INT >= 30) {
            headroom = try { power.getThermalHeadroom(60).toDouble() } catch (_: Exception) { null }
        }
        return mapOf(
            "thermal_status" to thermalStatus,
            "thermal_headroom" to headroom,
        )
    }

    /* ---- confounders ----------------------------------------------------- */

    fun confounders(): Map<String, Any?> {
        val wifiEnabled = try {
            (appContext.getSystemService(Context.WIFI_SERVICE) as? WifiManager)?.isWifiEnabled == true
        } catch (_: Exception) { null }
        val btEnabled = try {
            if (Build.VERSION.SDK_INT >= 31) null else BluetoothAdapter.getDefaultAdapter()?.isEnabled == true
        } catch (_: Exception) { null }
        return mapOf(
            "screen_state" to (if (power.isInteractive) "on" else "off"),
            "wifi_state" to when (wifiEnabled) { true -> "on"; false -> "off"; null -> null },
            "bluetooth_state" to when (btEnabled) { true -> "on"; false -> "off"; null -> null },
            "airplane_mode" to (if (Settings.Global.getInt(appContext.contentResolver, Settings.Global.AIRPLANE_MODE_ON, 0) != 0) 1 else 0),
        )
    }

    companion object {
        private fun mapNetworkType(t: Int): String = when (t) {
            TelephonyManager.NETWORK_TYPE_NR -> "NR"
            TelephonyManager.NETWORK_TYPE_LTE -> "LTE"
            TelephonyManager.NETWORK_TYPE_UMTS -> "UMTS"
            TelephonyManager.NETWORK_TYPE_GSM -> "GSM"
            else -> "OTHER($t)"
        }
    }
}
