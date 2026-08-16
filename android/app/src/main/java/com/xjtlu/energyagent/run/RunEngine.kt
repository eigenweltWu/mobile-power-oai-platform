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

/** Preloaded experiment plan (from the PC over the USB control channel).
 *
 * Standard phases from the PC: idle(idleSeconds) → loaded(collectionSeconds)
 * → idle(0 = continuous until stop). The phone applies its local idle-seconds
 * setting on top of the plan before arming (see ExperimentService.applyLocalIdleOverride).
 */
data class ExperimentPlan(
    val experimentId: String,
    val runId: String,
    val conditionId: String,
    val environment: String,
    val startDelaySeconds: Double,
    val idleSeconds: Double = 15.0,
    val collectionSeconds: Double = 120.0,
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
            val idleSeconds = json.optDouble("idleSeconds", 15.0)
            val collectionSeconds = json.optDouble("collectionSeconds", 120.0)
            return ExperimentPlan(
                experimentId = json.optString("experimentId"),
                runId = json.optString("runId"),
                conditionId = json.optString("conditionId"),
                environment = json.optString("environment", "AC"),
                startDelaySeconds = json.optDouble("startDelaySeconds", 0.0),
                idleSeconds = idleSeconds,
                collectionSeconds = collectionSeconds,
                phases = phases.ifEmpty {
                    listOf(Phase("idle", idleSeconds), Phase("loaded", collectionSeconds), Phase("idle", 0.0))
                }
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
     *
     * A phase with ``durationSeconds == 0`` runs **continuously** until the user
     * stops the experiment — it never transitions into COMPLETE on its own.
     * This is how the trailing "idle" tail behaves: after the loaded test
     * finishes the phone returns to idle and keeps recording indefinitely.
     */
    fun update(nowElapsedNs: Long, onMarker: (String) -> Unit): String? {
        if (state != State.ARMED && state != State.RUNNING) return currentPhase
        if (nowElapsedNs < startElapsedNs) return null

        if (state == State.ARMED) {
            state = State.RUNNING
            currentPhaseIndex = 0
            onMarker("${phaseNames[0]}_START")
        }

        // advance through phase boundaries. A phase with duration 0 is the
        // "run forever" sentinel — never cross into the next phase from it.
        while (currentPhaseIndex + 1 < phaseStartsNs.size &&
            nowElapsedNs >= phaseStartsNs[currentPhaseIndex + 1]) {
            val leavingDur = plan?.phases?.get(currentPhaseIndex)?.durationSeconds ?: 0.0
            if (leavingDur == 0.0) break   // continuous phase — keep recording
            val leaving = currentPhaseIndex
            currentPhaseIndex++
            onMarker("${phaseNames[leaving].uppercase()}_END")
            onMarker("${phaseNames[currentPhaseIndex].uppercase()}_START")
        }

        // completion: only when the LAST phase has a finite (>0) duration and
        // we've passed its end. A 0-duration tail never auto-completes.
        if (currentPhaseIndex == phaseStartsNs.size - 1 && state == State.RUNNING) {
            val lastStart = phaseStartsNs[currentPhaseIndex]
            val lastDurNs = ((plan?.phases?.get(currentPhaseIndex)?.durationSeconds ?: 0.0) * 1e9).toLong()
            if (lastDurNs > 0 && nowElapsedNs >= lastStart + lastDurNs) {
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

    /**
     * Return to a fresh IDLE state — called after the run is fully torn down
     * (markers recorded, workload stopped). Without this the engine would sit
     * in ABORTED forever, which blocks BOTH the second experiment's monitoring
     * UI (isMonitoring requires IDLE) and the new sync-confirm arm
     * (handleSyncConfirm ignores non-IDLE states as duplicates).
     */
    fun reset() {
        state = State.IDLE
        plan = null
        currentPhaseIndex = -1
        phaseStartsNs = LongArray(0)
        phaseNames = arrayOf()
        armElapsedNs = 0
        startElapsedNs = 0
    }
}
