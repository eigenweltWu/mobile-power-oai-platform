package com.xjtlu.energyagent

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray
import org.json.JSONObject

/**
 * Lightweight store for pushed tasks and collection records (SharedPreferences,
 * so no Room migration is needed). Task metadata is small.
 */
object TaskStore {
    private const val PREFS = "task_store"
    private const val KEY_TASKS = "tasks"
    private const val KEY_COLLECTIONS = "collections"

    private fun prefs(ctx: Context): SharedPreferences =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun listTasks(ctx: Context): List<JSONObject> {
        val raw = prefs(ctx).getString(KEY_TASKS, "[]") ?: "[]"
        val arr = JSONArray(raw)
        return (0 until arr.length()).map { arr.getJSONObject(it) }
    }

    fun addTask(ctx: Context, task: JSONObject) {
        val prefs = prefs(ctx)
        val arr = JSONArray(prefs.getString(KEY_TASKS, "[]") ?: "[]")
        // upsert by experimentId
        val eid = task.optString("experimentId")
        var i = 0
        while (i < arr.length()) {
            if (arr.getJSONObject(i).optString("experimentId") == eid) arr.remove(i) else i++
        }
        arr.put(task)
        prefs.edit().putString(KEY_TASKS, arr.toString()).apply()
    }

    fun recordCollection(ctx: Context, experimentId: String, hostname: String): JSONObject {
        val prefs = prefs(ctx)
        val arr = JSONArray(prefs.getString(KEY_COLLECTIONS, "[]") ?: "[]")
        val count = (0 until arr.length()).count {
            arr.getJSONObject(it).optString("experimentId") == experimentId
        } + 1
        val rec = JSONObject().apply {
            put("experimentId", experimentId)
            put("hostname", hostname)
            put("collectedUtcMs", System.currentTimeMillis())
            put("count", count)
        }
        arr.put(rec)
        prefs.edit().putString(KEY_COLLECTIONS, arr.toString()).apply()
        return rec
    }

    fun collectionCount(ctx: Context, experimentId: String): Int {
        val arr = JSONArray(prefs(ctx).getString(KEY_COLLECTIONS, "[]") ?: "[]")
        return (0 until arr.length()).count {
            arr.getJSONObject(it).optString("experimentId") == experimentId
        }
    }
}
