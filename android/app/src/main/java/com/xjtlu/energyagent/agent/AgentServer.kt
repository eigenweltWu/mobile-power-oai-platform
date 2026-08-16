package com.xjtlu.energyagent.agent

import android.content.Context
import android.content.Intent
import android.os.SystemClock
import com.xjtlu.energyagent.AgentState
import com.xjtlu.energyagent.TaskStore
import com.xjtlu.energyagent.db.AppDatabase
import com.xjtlu.energyagent.export.CsvExporter
import com.xjtlu.energyagent.run.ExperimentPlan
import com.xjtlu.energyagent.run.RunEngine
import com.xjtlu.energyagent.service.ExperimentService
import fi.iki.elonen.NanoHTTPD
import kotlinx.coroutines.runBlocking
import org.json.JSONArray
import org.json.JSONObject

/**
 * Loopback + PDU-IP HTTP control server. Binds 0.0.0.0 so it is reachable both
 * via the USB tunnel (adb forward -> 127.0.0.1) and over the 5G PDU interface
 * (10.0.1.x / 10.0.0.x) for the no-USB downlink/uplink handshake. Used only
 * before/after an experiment, never for cloud/Wi-Fi sync during the run.
 */
class AgentServer(private val context: Context) : NanoHTTPD("0.0.0.0", 8420) {

    override fun serve(session: IHTTPSession): Response {
        val uri = session.uri.trimEnd('/')
        return try {
            when {
                session.method == Method.GET && uri == "/agent/status" -> json(ok(status()))
                session.method == Method.GET && uri == "/agent/time" -> json(ok(time()))
                session.method == Method.GET && uri == "/agent/export" -> json(ok(export()))
                session.method == Method.POST && uri == "/agent/session" -> json(ok(session(session)))
                session.method == Method.POST && uri == "/agent/arm" -> json(ok(arm(session)))
                session.method == Method.POST && uri == "/agent/abort" -> json(ok(abort()))
                session.method == Method.GET && uri == "/agent/tasks" -> json(ok(tasks()))
                session.method == Method.POST && uri == "/agent/tasks" -> json(ok(addTask(session)))
                session.method == Method.POST && uri == "/agent/task/start" -> json(ok(taskStart(session)))
                session.method == Method.POST && uri == "/agent/task/stop" -> json(ok(taskStop()))
                session.method == Method.POST && uri == "/agent/downlink" -> json(ok(downlink(session)))
                session.method == Method.POST && uri == "/agent/sync/confirm" -> json(ok(syncConfirm(session)))
                session.method == Method.POST && uri == "/agent/collected" -> json(ok(collected(session)))
                else -> json(error(404, "not found"))
            }
        } catch (e: Exception) {
            json(error(500, e.message ?: "error"))
        }
    }

    /* ---- handlers -------------------------------------------------------- */

    private fun status(): JSONObject = JSONObject().apply {
        put("state", AgentState.runEngine.state.name)
        put("experimentId", AgentState.currentPlan?.experimentId ?: JSONObject.NULL)
        put("runId", AgentState.currentPlan?.runId ?: JSONObject.NULL)
        put("conditionId", AgentState.currentPlan?.conditionId ?: JSONObject.NULL)
        put("phase", AgentState.runEngine.currentPhase ?: JSONObject.NULL)
        put("samplingHz", AgentState.samplingHz)
        put("serverStarted", AgentState.serverStarted)
    }

    /** t2 timestamp for NTP-style sync: reply as fast as possible. */
    private fun time(): JSONObject = JSONObject().apply {
        put("utcEpochMs", System.currentTimeMillis())
        put("elapsedRealtimeNs", SystemClock.elapsedRealtimeNanos())
    }

    private fun session(session: IHTTPSession): JSONObject {
        val body = readBody(session)
        val plan = JSONObject(body)
        // store pending plan; the PC then calls /agent/arm to start.
        AgentState.currentPlan = com.xjtlu.energyagent.run.ExperimentPlan.fromJson(plan)
        return JSONObject().apply {
            put("ok", true)
            put("runId", AgentState.currentPlan?.runId ?: JSONObject.NULL)
            put("state", "PLAN_LOADED")
        }
    }

    private fun arm(session: IHTTPSession): JSONObject {
        val plan = AgentState.currentPlan
        if (plan == null) {
            val body = readBody(session)
            AgentState.currentPlan = ExperimentPlan.fromJson(JSONObject(body))
        }
        val p = AgentState.currentPlan ?: throw IllegalStateException("no plan loaded")
        val intent = Intent(context, ExperimentService::class.java)
            .setAction(ExperimentService.ACTION_ARM)
            .putExtra(ExperimentService.EXTRA_PLAN_JSON, planToJson(p))
        context.startForegroundService(intent)
        return JSONObject().apply { put("ok", true); put("state", "ARMED"); put("runId", p.runId) }
    }

    /** Serialise a plan to the JSON wire format ExperimentPlan.fromJson expects. */
    private fun planToJson(plan: ExperimentPlan): String = JSONObject().apply {
        put("experimentId", plan.experimentId)
        put("runId", plan.runId)
        put("conditionId", plan.conditionId)
        put("environment", plan.environment)
        put("startDelaySeconds", plan.startDelaySeconds)
        val arr = JSONArray()
        for (ph in plan.phases) arr.put(JSONObject().put("name", ph.name).put("durationSeconds", ph.durationSeconds))
        put("phases", arr)
    }.toString()

    private fun abort(): JSONObject {
        val intent = Intent(context, ExperimentService::class.java).setAction(ExperimentService.ACTION_STOP)
        context.startService(intent)
        return JSONObject().apply { put("ok", true); put("state", "ABORTED") }
    }

    /* ---- new task-flow handlers ------------------------------------------ */

    private fun tasks(): JSONObject {
        val arr = org.json.JSONArray()
        for (t in TaskStore.listTasks(context)) arr.put(t)
        return JSONObject().apply { put("ok", true); put("experiments", arr) }
    }

    private fun addTask(session: IHTTPSession): JSONObject {
        val task = JSONObject(readBody(session))
        TaskStore.addTask(context, task)
        return JSONObject().apply { put("ok", true); put("experimentId", task.optString("experimentId")) }
    }

    private fun taskStart(session: IHTTPSession): JSONObject {
        val eid = JSONObject(readBody(session)).optString("experimentId")
        // begin environment monitoring (the ExperimentService monitors signal and
        // records airplane-mode toggles); record the start timestamp. The phase
        // machine stays IDLE until the PC completes the sync handshake.
        val intent = Intent(context, ExperimentService::class.java)
            .setAction(ExperimentService.ACTION_START_SERVICE)
            .putExtra(ExperimentService.EXTRA_EXPERIMENT_ID, eid)
        context.startForegroundService(intent)
        TaskStore.recordCollection(context, "start_$eid", "phone")
        return JSONObject().apply { put("ok", true); put("experimentId", eid); put("state", "MONITORING") }
    }

    private fun taskStop(): JSONObject {
        val intent = Intent(context, ExperimentService::class.java).setAction(ExperimentService.ACTION_STOP)
        context.startService(intent)
        return JSONObject().apply { put("ok", true); put("state", "STOPPED"); put("stopUtcMs", System.currentTimeMillis()) }
    }

    /** Downlink ping: reply with phone recv/send timestamps (the uplink ACK). */
    private fun downlink(session: IHTTPSession): JSONObject {
        val body = JSONObject(readBody(session))
        val seq = body.optInt("seq")
        val pcSendMs = body.optLong("pcSendMs")
        val recv = System.currentTimeMillis()
        val recvElapsed = SystemClock.elapsedRealtimeNanos()
        val send = System.currentTimeMillis()
        // Persist the ACK asynchronously; never block the reply (latency-sensitive).
        AgentState.service?.recordDownlinkAck(seq, pcSendMs, recv, send, recvElapsed)
        return JSONObject().apply {
            put("ok", true)
            put("seq", seq)
            put("phoneRecvMs", recv)
            put("phoneSendMs", send)
            put("phoneElapsedNs", recvElapsed)
        }
    }

    /**
     * PC sync-confirm: the PC received the first uplink ACK, captured the gNB
     * data timestamp, and now hands the phone both clocks. The phone computes
     * the communication delay and auto-arms the run (full baseline→active→tail).
     * Idempotent: a duplicate confirm while already armed is a no-op.
     */
    private fun syncConfirm(session: IHTTPSession): JSONObject {
        val body = JSONObject(readBody(session))
        val pcTs = body.optLong("pc_timestamp_ms")
        val gnbTs = body.optLong("gnb_data_timestamp_ms")
        val phoneTs = System.currentTimeMillis()
        val phoneElapsed = SystemClock.elapsedRealtimeNanos()
        val delay = (phoneTs - pcTs).toDouble()

        // Idempotency: if the engine already left IDLE, don't re-arm.
        if (AgentState.runEngine.state != RunEngine.State.IDLE) {
            return JSONObject().apply {
                put("ok", true)
                put("already_armed", true)
                put("phone_timestamp_ms", phoneTs)
                put("delay_ms", delay)
            }
        }

        // Resolve the plan: body.plan (primary) or the already-loaded plan (fallback).
        val planObj = body.optJSONObject("plan")
        if (planObj != null) {
            AgentState.currentPlan = ExperimentPlan.fromJson(planObj)
        }
        val plan = AgentState.currentPlan
            ?: throw IllegalStateException("no plan for sync-confirm (push+start the task first)")

        val intent = Intent(context, ExperimentService::class.java)
            .setAction(ExperimentService.ACTION_SYNC_CONFIRM)
            .putExtra(ExperimentService.EXTRA_PC_TS, pcTs)
            .putExtra(ExperimentService.EXTRA_GNB_TS, gnbTs)
            .putExtra(ExperimentService.EXTRA_PHONE_TS, phoneTs)
            .putExtra(ExperimentService.EXTRA_PHONE_ELAPSED, phoneElapsed)
            .putExtra(ExperimentService.EXTRA_DELAY, delay)
            .putExtra(ExperimentService.EXTRA_PLAN_JSON, planToJson(plan))
        context.startForegroundService(intent)
        return JSONObject().apply {
            put("ok", true)
            put("state", "ARMING")
            put("phone_timestamp_ms", phoneTs)
            put("delay_ms", delay)
            put("gnb_data_timestamp_ms", gnbTs)
        }
    }

    private fun collected(session: IHTTPSession): JSONObject {
        val body = JSONObject(readBody(session))
        val rec = TaskStore.recordCollection(context, body.optString("experimentId"), body.optString("hostname", "pc"))
        return JSONObject().apply { put("ok", true); put("collection", rec) }
    }

    private fun export(): JSONObject {
        val db = AppDatabase.get(context)
        val plan = AgentState.currentPlan
        val runId = plan?.runId
        val files = JSONObject()
        if (runId != null) {
            val samples = runBlocking { db.samples().byRun(runId) }
            val markers = runBlocking { db.markers().byRun(runId) }
            val anchors = runBlocking { db.sync().byDirection("before") + db.sync().byDirection("after") }
            files.put("phone_samples.csv", CsvExporter.samplesCsv(samples))
            files.put("phone_events.csv", CsvExporter.eventsCsv(markers))
            files.put("phone_sync.json", CsvExporter.syncJson(anchors))
            files.put("phone_session.json", sessionJson(db))
        }
        return JSONObject().apply { put("ok", true); put("files", files) }
    }

    private fun sessionJson(db: AppDatabase): String = runBlocking {
        val device = db.meta().device()
        val run = db.meta().latestRun()
        JSONObject().apply {
            put("device", JSONObject().apply {
                put("device_id", device?.deviceId)
                put("manufacturer", device?.manufacturer)
                put("model", device?.model)
                put("codename", device?.codename)
                put("android_version", device?.androidVersion)
                put("sdk_int", device?.sdkInt)
                put("build_fingerprint", device?.buildFingerprint)
                put("app_version", device?.appVersion)
                put("battery_health", device?.batteryHealth)
                put("battery_capacity_uah", device?.batteryCapacityUah)
            })
            put("run", JSONObject().apply {
                put("run_id", run?.runId)
                put("experiment_id", run?.experimentId)
                put("condition_id", run?.conditionId)
                put("state", run?.state)
                put("sampling_hz", AgentState.samplingHz)
            })
        }.toString(2)
    }

    /* ---- helpers --------------------------------------------------------- */

    private fun readBody(session: IHTTPSession): String {
        val files = HashMap<String, String>()
        session.parseBody(files)
        return files["postData"] ?: ""
    }

    private fun ok(payload: JSONObject) = JSONObject().put("ok", true).let { o ->
        for (k in payload.keys()) o.put(k, payload.get(k))
        o
    }

    private fun error(code: Int, message: String) = JSONObject().apply {
        put("ok", false); put("error", message); put("status", code)
    }

    private fun json(obj: JSONObject): Response =
        newFixedLengthResponse(Response.Status.OK, "application/json; charset=utf-8", obj.toString())
}
