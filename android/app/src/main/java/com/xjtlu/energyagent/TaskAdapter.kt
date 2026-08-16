package com.xjtlu.energyagent

import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.recyclerview.widget.RecyclerView
import org.json.JSONObject

/**
 * Adapter for the home task list. Each task is a JSON object pushed by the PC
 * over the USB control channel and stored in [TaskStore]. Fields follow the
 * ExperimentPlan schema (experimentId, runId, conditionId, environment, phases).
 */
class TaskAdapter(
    private var tasks: List<JSONObject> = emptyList(),
    private val statusOf: (String) -> TaskStatus,
    private val onClick: (String) -> Unit
) : RecyclerView.Adapter<TaskAdapter.TaskVH>() {

    enum class TaskStatus(val labelRes: Int, val colorRes: Int) {
        PENDING(R.string.task_status_pending, R.color.status_pending),
        ARMED(R.string.task_status_armed, R.color.status_armed),
        RUNNING(R.string.task_status_running, R.color.status_running),
        COMPLETE(R.string.task_status_complete, R.color.status_complete),
        ABORTED(R.string.task_status_aborted, R.color.status_aborted)
    }

    fun submit(list: List<JSONObject>) {
        tasks = list
        notifyDataSetChanged()
    }

    override fun onCreateViewHolder(parent: ViewGroup, viewType: Int): TaskVH {
        val v = LayoutInflater.from(parent.context)
            .inflate(R.layout.item_task, parent, false)
        return TaskVH(v)
    }

    override fun getItemCount(): Int = tasks.size

    override fun onBindViewHolder(holder: TaskVH, position: Int) {
        holder.bind(tasks[position])
    }

    inner class TaskVH(itemView: View) : RecyclerView.ViewHolder(itemView) {
        private val title: TextView = itemView.findViewById(R.id.task_title)
        private val meta: TextView = itemView.findViewById(R.id.task_meta)
        private val phases: TextView = itemView.findViewById(R.id.task_phases)
        private val status: TextView = itemView.findViewById(R.id.task_status)

        fun bind(task: JSONObject) {
            val ctx = itemView.context
            val eid = task.optString("experimentId", ctx.getString(R.string.task_load_failed))
            val runId = task.optString("runId", "—")
            val condId = task.optString("conditionId", "—")
            val env = task.optString("environment", "AC")

            title.text = eid
            meta.text = "run: $runId   cond: $condId   env: $env"

            val phasesText = buildPhasesSummary(task)
            phases.text = if (phasesText.isNotEmpty())
                "${ctx.getString(R.string.task_card_phases_label)}: $phasesText"
            else ""

            val st = statusOf(eid)
            status.text = ctx.getString(st.labelRes)
            status.setTextColor(ctx.getColor(st.colorRes))

            itemView.setOnClickListener { onClick(eid) }
        }

        private fun buildPhasesSummary(task: JSONObject): String {
            val arr = task.optJSONArray("phases") ?: return ""
            val parts = mutableListOf<String>()
            for (i in 0 until arr.length()) {
                val p = arr.optJSONObject(i) ?: continue
                val name = p.optString("name", "phase$i")
                val dur = p.optDouble("durationSeconds", 0.0)
                parts += "$name ${"%.0f".format(dur)}s"
            }
            return parts.joinToString(" → ")
        }
    }
}
