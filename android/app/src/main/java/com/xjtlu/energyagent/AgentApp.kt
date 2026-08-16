package com.xjtlu.energyagent

import android.app.Application
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.BatteryManager
import android.os.Build
import com.xjtlu.energyagent.agent.AgentServer
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.db.DeviceInfoEntity
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch

class AgentApp : Application() {
    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)

    override fun onCreate() {
        super.onCreate()
        scope.launch { recordDeviceInfo() }
        try {
            AgentServer(this).start()
            AgentState.serverStarted = true
        } catch (_: Exception) {
        }
    }

    private suspend fun recordDeviceInfo() {
        val db = AppDatabase.get(this)
        val bm = getSystemService(Context.BATTERY_SERVICE) as BatteryManager
        val capacity = try { bm.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY) } catch (_: Exception) { -1 }
        val health = try {
            val intent = registerReceiver(null, IntentFilter(Intent.ACTION_BATTERY_CHANGED))
            intent?.getIntExtra(BatteryManager.EXTRA_HEALTH, -1)
        } catch (_: Exception) { -1 }
        db.meta().upsertDevice(
            DeviceInfoEntity(
                deviceId = Build.FINGERPRINT ?: (Build.MODEL ?: "unknown"),
                manufacturer = Build.MANUFACTURER,
                model = Build.MODEL,
                codename = Build.DEVICE,
                androidVersion = Build.VERSION.RELEASE,
                sdkInt = Build.VERSION.SDK_INT,
                buildFingerprint = Build.FINGERPRINT,
                appVersion = "0.1.0",
                batteryHealth = health?.toString(),
                batteryCapacityUah = if (capacity > 0) capacity * 1000 else null  // mAh -> uAh
            )
        )
    }
}
