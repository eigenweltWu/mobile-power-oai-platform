package com.xjtlu.energyagent

import com.xjtlu.energyagent.run.ExperimentPlan
import com.xjtlu.energyagent.run.RunEngine
import com.xjtlu.energyagent.run.WorkloadEngine
import com.xjtlu.energyagent.service.ExperimentService

/** One point for the on-screen rolling chart (last ~1 minute). */
data class DisplaySample(
    val elapsedNs: Long,
    val currentUa: Double?,
    val voltageMv: Double?,
    val powerW: Double?,
    val rsrpDbm: Double?,
    val temperatureC: Double?
)

/** Shared runtime state between the service, agent server and UI. */
object AgentState {
    val runEngine = RunEngine()
    @Volatile var workload: WorkloadEngine? = null
    @Volatile var service: ExperimentService? = null
    @Volatile var samplingHz: Int = 5

    @Volatile var currentPlan: ExperimentPlan? = null

    /** ExperimentId the phone is currently monitoring (env monitoring, before
     *  sync-confirm). Set on ACTION_START_SERVICE; cleared on stop. */
    @Volatile var monitoringExperimentId: String? = null

    /** Communication delay (ms) measured at sync-confirm (phone_now - pc_ts). */
    @Volatile var syncDelayMs: Double? = null

    @Volatile var serverStarted = false

    private const val MAX_DISPLAY = 360  // ~1 min at 5 Hz plus margin
    private val displayBuffer = ArrayDeque<DisplaySample>()

    val displaySamples: List<DisplaySample>
        get() = synchronized(displayBuffer) { displayBuffer.toList() }

    fun recordDisplay(s: DisplaySample) {
        synchronized(displayBuffer) {
            displayBuffer.addLast(s)
            while (displayBuffer.size > MAX_DISPLAY) displayBuffer.removeFirst()
        }
    }

    /** Drop the on-screen rolling buffer (e.g. when a new task is started). */
    fun clearDisplay() {
        synchronized(displayBuffer) { displayBuffer.clear() }
    }
}
