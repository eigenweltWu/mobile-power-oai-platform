package com.xjtlu.energyagent

import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.view.View
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AlertDialog
import androidx.appcompat.app.AppCompatActivity
import androidx.recyclerview.widget.LinearLayoutManager
import androidx.recyclerview.widget.RecyclerView
import com.xjtlu.energyagent.run.RunEngine
import com.xjtlu.energyagent.service.ExperimentService

/**
 * Home screen: a card list of tasks pushed from the PC over the USB control
 * channel. Tapping a card opens [MainActivity] (task detail). The foreground
 * service is kept running so the loopback control server stays reachable for
 * the PC to push new tasks — but no telemetry is displayed here.
 */
class TaskListActivity : AppCompatActivity() {

    private lateinit var recycler: RecyclerView
    private lateinit var emptyState: TextView
    private lateinit var adapter: TaskAdapter

    private val handler = Handler(Looper.getMainLooper())
    private val refreshLoop = object : Runnable {
        override fun run() {
            refreshTaskList()
            handler.postDelayed(this, 1000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_task_list)

        recycler = findViewById(R.id.recycler_tasks)
        emptyState = findViewById(R.id.empty_state)

        adapter = TaskAdapter(
            statusOf = ::statusForTask,
            onClick = ::openTask
        )
        recycler.layoutManager = LinearLayoutManager(this)
        recycler.adapter = adapter

        findViewById<Button>(R.id.btn_help).setOnClickListener { showHelp() }

        // Keep the foreground service alive so the USB control server (started
        // in AgentApp) can keep receiving plans; the service only samples when
        // a plan is armed from the detail screen.
        startForegroundService(
            Intent(this, ExperimentService::class.java)
                .setAction(ExperimentService.ACTION_START_SERVICE)
        )
    }

    /** Resolve a task's status from the global RunEngine state. */
    private fun statusForTask(experimentId: String): TaskAdapter.TaskStatus {
        val plan = AgentState.currentPlan
        val engine = AgentState.runEngine
        val isThisTask = plan?.experimentId == experimentId
        return when {
            !isThisTask -> TaskAdapter.TaskStatus.PENDING
            engine.state == RunEngine.State.ARMED -> TaskAdapter.TaskStatus.ARMED
            engine.state == RunEngine.State.RUNNING -> TaskAdapter.TaskStatus.RUNNING
            engine.state == RunEngine.State.COMPLETE -> TaskAdapter.TaskStatus.COMPLETE
            engine.state == RunEngine.State.ABORTED -> TaskAdapter.TaskStatus.ABORTED
            else -> TaskAdapter.TaskStatus.PENDING
        }
    }

    private fun openTask(experimentId: String) {
        val intent = Intent(this, MainActivity::class.java)
            .putExtra(EXTRA_EXPERIMENT_ID, experimentId)
        startActivity(intent)
    }

    private fun refreshTaskList() {
        val tasks = TaskStore.listTasks(this)
        adapter.submit(tasks)
        emptyState.visibility = if (tasks.isEmpty()) View.VISIBLE else View.GONE
        recycler.visibility = if (tasks.isEmpty()) View.GONE else View.VISIBLE
    }

    private fun showHelp() {
        val help = """
            【5G Energy Experiment Agent 使用帮助】

            1. 任务列表：主页显示 PC 通过 USB 下发的实验任务，每个任务一张卡片。

            2. 任务流程：点击任务卡片进入详情 → 点击「开始任务」开始采集（功率 / RSSI 等）→ 实时显示当前所处阶段（baseline / active / tail）→ 完成后可导出数据。

            3. 离线实验：开始任务后可拔掉 USB、熄屏，前台服务 + 唤醒锁继续按 monotonic 时钟执行各阶段。

            4. 采集内容：电池（电流/电压/SOC/温度）、NR（SS-RSRP/RSRQ/SINR）、thermal、confounder（屏幕/充电/Wi-Fi/蓝牙/飞行模式）。

            5. 权限：READ_PHONE_STATE 用于信号强度；小区身份需手动授予 ACCESS_FINE_LOCATION。

            6. 数据：本地 Room 存储，实验后由 PC 经 /agent/export 拉取（USB-only 通道），不经过 Wi-Fi/云端。
        """.trimIndent()
        AlertDialog.Builder(this)
            .setTitle("使用帮助")
            .setMessage(help)
            .setPositiveButton("关闭", null)
            .show()
    }

    override fun onResume() {
        super.onResume()
        refreshTaskList()
        handler.post(refreshLoop)
    }

    override fun onPause() {
        super.onPause()
        handler.removeCallbacks(refreshLoop)
    }

    companion object {
        const val EXTRA_EXPERIMENT_ID = "experiment_id"
    }
}
