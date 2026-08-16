package com.xjtlu.energyagent.run

import org.json.JSONArray
import org.json.JSONObject

/** One phase in the offline plan. */
data class Phase(val name: String, val durationSeconds: Double)

/** Snapshot of the current phase progress, for UI display. */
data class PhaseProgress(
    val name: String,
    val index: Int,          // 0-based index of the current phase
    val total: Int,          // total number of phases in the plan
    val elapsedSeconds: Double,
    val durationSeconds: Double,
    val remainingSeconds: Double
)

/** Preloaded experiment plan (from the PC over the USB control channel). */
data class ExperimentPlan(
    val experimentId: String,
    val runId: String,
    val conditionId: String,
    val environment: String,
    val startDelaySeconds: Double,
    val phases: List<Phase>
) {
    companion object {
        fun fromJson(json: JSONObject): ExperimentPlan {
            val phases = mutableListOf<Phase>()
            val arr: JSONArray = json.optJSONArray("phases") ?: JSONArray()
            for (i in 0 until arr.length()) {
                val p = arr.optJSONObject(i) ?: continue
                phases += Phase(p.optString("name", "phase$i"), p.optDouble("durationSeconds", 0.0))
            }
            return ExperimentPlan(
                experimentId = json.optString("experimentId"),
                runId = json.optString("runId"),
                conditionId = json.optString("conditionId"),
                environment = json.optString("environment", "AC"),
                startDelaySeconds = json.optDouble("startDelaySeconds", 30.0),
                phases = phases.ifEmpty { listOf(Phase("baseline", 30.0), Phase("active", 120.0), Phase("tail", 60.0)) }
            )
        }
    }
}

/**
 * Offline run state machine driven by the monotonic [android.os.SystemClock.elapsedRealtimeNanos]
 * clock — never wall clock (task §17). Phase transitions write markers via [onMarker].
 */
class RunEngine {
    enum class State { IDLE, ARMED, RUNNING, COMPLETE, ABORTED }

    @Volatile var state: State = State.IDLE
        private set
    @Volatile var plan: ExperimentPlan? = null
        private set

    // elapsed-realtime boundaries (ns)
    private var armElapsedNs: Long = 0
    private var startElapsedNs: Long = 0
    private var phaseStartsNs: LongArray = LongArray(0)
    private var phaseNames: Array<String> = arrayOf()
    private var currentPhaseIndex = -1

    val currentPhase: String?
        get() = if (currentPhaseIndex in phaseNames.indices) phaseNames[currentPhaseIndex] else null

    /** 0-based index of the current phase, or -1 if not in a phase yet. */
    val phaseIndex: Int
        get() = if (currentPhaseIndex in phaseNames.indices) currentPhaseIndex else -1

    /** Total number of phases in the armed plan (0 if not armed). */
    val phaseCount: Int
        get() = phaseNames.size

    /**
     * Snapshot of the current phase progress for UI display. Returns null when
     * the engine is not RUNNING/COMPLETE or no phase is active.
     */
    fun phaseProgress(nowElapsedNs: Long): PhaseProgress? {
        if (state == State.IDLE || state == State.ARMED) return null
        if (currentPhaseIndex !in phaseNames.indices) return null
        val start = phaseStartsNs[currentPhaseIndex]
        val dur = plan?.phases?.get(currentPhaseIndex)?.durationSeconds ?: 0.0
        val elapsedSec = ((nowElapsedNs - start) / 1e9).coerceAtLeast(0.0)
        val remainingSec = (dur - elapsedSec).coerceAtLeast(0.0)
        return PhaseProgress(
            name = phaseNames[currentPhaseIndex],
            index = currentPhaseIndex,
            total = phaseNames.size,
            elapsedSeconds = elapsedSec,
            durationSeconds = dur,
            remainingSeconds = remainingSec
        )
    }

    fun arm(plan: ExperimentPlan, nowElapsedNs: Long, onMarker: (String) -> Unit) {
        this.plan = plan
        this.armElapsedNs = nowElapsedNs
        this.startElapsedNs = nowElapsedNs + (plan.startDelaySeconds * 1e9).toLong()
        this.currentPhaseIndex = -1
        this.state = State.ARMED
        phaseStartsNs = LongArray(plan.phases.size)
        phaseNames = Array(plan.phases.size) { plan.phases[it].name.uppercase() }
        var t = startElapsedNs
        for (i in plan.phases.indices) {
            phaseStartsNs[i] = t
            t += (plan.phases[i].durationSeconds * 1e9).toLong()
        }
        onMarker("RUN_ARMED")
    }

    /**
     * Called every sample. Returns the current phase string (null if not yet started).
     * Fires phase-transition markers exactly once per boundary.
     */
    fun update(nowElapsedNs: Long, onMarker: (String) -> Unit): String? {
        if (state != State.ARMED && state != State.RUNNING) return currentPhase
        if (nowElapsedNs < startElapsedNs) return null

        if (state == State.ARMED) {
            state = State.RUNNING
            currentPhaseIndex = 0
            onMarker("BASELINE_START")
        }

        // advance through phase boundaries
        while (currentPhaseIndex + 1 < phaseStartsNs.size && nowElapsedNs >= phaseStartsNs[currentPhaseIndex + 1]) {
            val leaving = currentPhaseIndex
            currentPhaseIndex++
            val leavingName = phaseNames[leaving].uppercase()
            val enteringName = phaseNames[currentPhaseIndex].uppercase()
            onMarker("${leavingName}_END")
            onMarker("${enteringName}_START")
        }

        // completion: past the end of the last phase
        if (currentPhaseIndex == phaseStartsNs.size - 1) {
            val lastStart = phaseStartsNs[currentPhaseIndex]
            val lastDurNs = ((plan?.phases?.get(currentPhaseIndex)?.durationSeconds ?: 0.0) * 1e9).toLong()
            if (nowElapsedNs >= lastStart + lastDurNs && state == State.RUNNING) {
                onMarker("${phaseNames[currentPhaseIndex]}_END")
                onMarker("RUN_COMPLETE")
                state = State.COMPLETE
            }
        }
        return currentPhase
    }

    fun abort(onMarker: (String) -> Unit) {
        if (state == State.ARMED || state == State.RUNNING) {
            onMarker("RUN_ABORTED")
        }
        state = State.ABORTED
    }
}
