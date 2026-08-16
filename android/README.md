# 5G Energy Experiment Agent（安卓采集 App）

原生 Android App：离线能耗/信号采集执行器 + 实验执行器。正式实验窗口内**无 USB、无 Wi-Fi、不轮询
OAI/PC**，屏幕关闭仍持续采集；实验前后通过 USB 或 **5G PDU IP** 与控制平台通信。

## 技术栈

- Kotlin + Room（KSP）+ 前台 Service（`dataSync`）+ 部分唤醒锁
- 嵌入式 HTTP 控制服务（NanoHTTPD，绑定 `0.0.0.0:8420`，USB loopback 与 5G PDU IP 均可访问）
- minSdk 29 / targetSdk 34 / compileSdk 36

## 目录

```text
android/
├── app/build.gradle.kts          依赖与构建配置
├── app/src/main/AndroidManifest.xml
└── app/src/main/java/com/xjtlu/energyagent/
    ├── AgentApp.kt               Application：设备信息入库 + 启动 HTTP 服务
    ├── AgentState.kt             共享运行时状态 + 最近1分钟滚动缓冲
    ├── TaskStore.kt              任务列表 / 采集记录（SharedPreferences）
    ├── TaskListActivity.kt       主页：任务卡片列表（launcher）
    ├── TaskAdapter.kt            任务卡片 RecyclerView 适配器
    ├── MainActivity.kt           任务详情页：开始任务 + 阶段进度 + 遥测图表
    ├── TelemetryChart.kt         最近60s实时折线图（功率/电流/电压/RSRP/温度）
    ├── db/AppDatabase.kt         Room 实体/DAO/数据库
    ├── telemetry/TelemetryCollector.kt  battery/NR/thermal/confounders
    ├── run/RunEngine.kt          离线 run 状态机 + plan + PhaseProgress 阶段进度
    ├── run/WorkloadEngine.kt     UL/DL 大流量（绑定 cellular + TrafficStats 核验）
    ├── service/ExperimentService.kt 前台服务 + 5Hz采样 + 飞行模式刷新 + wake lock
    ├── agent/AgentServer.kt      HTTP 控制服务（USB + 5G 双通道）
    └── export/CsvExporter.kt     CSV/JSON 导出
```

## 界面与任务流程

主页为**任务卡片列表**（`TaskListActivity`，launcher），每个任务一张卡片，显示 experimentId、
run/condition/env、阶段摘要与状态徽章（待开始 / 监测中 / 运行中 / 已完成 / 已中止）。无任务时
提示「请通过 USB 从 PC 下发实验 plan」。

1. 点击任务卡片 → 进入**任务详情页**（`MainActivity`，传 `experimentId`）。
2. 详情页初始只显示任务信息卡片 + 「开始任务」按钮；功率 / RSSI / 图表 / 阶段进度均隐藏。
3. 点击「开始任务」→ 向 `ExperimentService` 发送 **`ACTION_START_SERVICE`**，
   **只进入环境监测模式**（`monitoringExperimentId` 标记，`RunEngine` 仍为 `IDLE`）：
   - 启动 5Hz 遥测采集，显示实时图表（功率/电流/电压/SS-RSRP/温度可切换）与状态文本；
   - 阶段进度条隐藏（尚未 ARM）；
   - 按钮切换为「停止任务」，状态提示「等待对时中…」。
4. **对时完成（收到 PC 的 sync-confirm）→ 自动 ARM**：
   `ExperimentService.handleSyncConfirm()` 写入 SyncAnchor + SYNC_CONFIRM marker 后
   调用 `RunEngine.arm(plan)`。UI 切换为「已同步，延迟 Xms」，顶部显示
   **阶段进度条**：「阶段 i/N: \<phase\>」+「已用 Xs / Ys」+ 百分比，
   由 `RunEngine.phaseProgress(nowNs)` 计算；ARMED 倒计时阶段显示「准备中」。
5. **三阶段采集**：
   | 阶段   | 默认 | 含义 |
   |--------|-----|------|
   | baseline | 30s  | 空闲 / 轻负载，基准功耗与信号的对照期 |
   | active   | 120s | `UL_CBR` 大流量上行（绑定蜂窝，5Mbps），确保高资源占用率 — 核心测量期 |
   | tail     | 60s  | 负载结束后的功耗与信号衰减回落期 |
   阶段时长由 plan 的 `phases` 字段决定：PC 端 `task_flow.py:DEFAULT_PHASES` 为权威来源；
   手机端兜底为 `RunEngine.kt:ExperimentPlan.fromJson` 的 `baseline(30)/active(120)/tail(60)`。
6. **无信号自动刷新**：详情页可点齿轮按钮修改 `no_signal_seconds`（默认 60s，最小 5s）。
   `monitorSignal()` 发现持续超时无 NR 信号 → 开飞行模式 3s → 关飞行模式，
   分别记录 `AIRPLANE_MODE_ON` / `AIRPLANE_MODE_OFF` marker，仍无信号则循环等 60s 再试。
7. 状态驱动按钮：
   - IDLE / ABORTED →「开始任务」；
   - 监测中 / ARMED / RUNNING →「停止任务」（记录 `PHONE_STOP` marker 含 utc + elapsed 双锚点）；
   - COMPLETE →「导出数据」。
   若另一任务运行中点「开始任务」会提示先停止。

相关布局：`res/layout/activity_task_list.xml`（主页）、`item_task.xml`（任务卡片）、
`activity_main.xml`（详情页，含阶段进度条）；配色与字符串见 `res/values/colors.xml`、`strings.xml`。

## 构建

用 Android Studio 打开 `android/`，或命令行：

```powershell
cd android
# 已生成 wrapper（gradlew / gradlew.bat / gradle-wrapper.jar）
.\gradlew.bat assembleDebug
# 产物：app/build/outputs/apk/debug/app-debug.apk
```

需要：本机 Android SDK（`platforms/android-36`、`build-tools/35·36`）+ JDK 17+（已验证 JDK 18）。

## 安装与授权

```powershell
adb install -r app\build\outputs\apk\debug\app-debug.apk
```

### MIUI 说明

`INSTALL_FAILED_USER_RESTRICTED` 表示 MIUI 拦截了「通过 USB 安装」。任选其一：
- 开发者选项里打开 **「USB 安装」**（部分版本需插 SIM + 登录小米账号）；
- 或把 APK 推到 `/sdcard/Download/` 后从文件管理器点装（走侧载，不经过 USB 安装）。

### 运行时权限（必做）

```powershell
adb shell pm grant com.xjtlu.energyagent android.permission.READ_PHONE_STATE        # NR 信号强度
adb shell pm grant com.xjtlu.energyagent android.permission.WRITE_SECURE_SETTINGS    # 飞行模式开关
adb shell pm grant com.xjtlu.energyagent android.permission.ACCESS_FINE_LOCATION     # 小区身份(PCI/NCI/TAC)
```

> `READ_PHONE_STATE` 用于 SS-RSRP/RSRQ/SINR；`ACCESS_FINE_LOCATION` 用于小区身份（一次性权限，
> 最好手动在系统设置里给「始终/仅使用中允许」）；`WRITE_SECURE_SETTINGS` 用于无信号时开关飞行模式。
> 缺权限时对应字段为 null（missing≠0），不会崩。

## 用户配置

| 配置项 | 存储位置 | 默认 | 说明 |
|--------|----------|------|------|
| `no_signal_seconds` | SharedPreferences `agent_settings` | 60 | 无 NR 信号持续多久后触发飞行模式刷新 |
| 采样率 `samplingHz` | `AgentState` | 5 | 5 Hz |

无信号刷新逻辑：持续 `no_signal_seconds` 无信号 → 开飞行模式 3 秒 → 关飞行模式，
记录 `AIRPLANE_MODE_ON/OFF` 时间戳标记；仍无信号则继续等待 60 秒再重复。

## Agent HTTP 端点（与控制平台契约一致，见 `phone_channel.py`）

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | `/agent/status` | 状态（state/experimentId/runId/phase/samplingHz / syncDelayMs） |
| GET  | `/agent/time` | 返回 `{"utcEpochMs":…, "elapsedRealtimeNs":…}`（对时 t2） |
| GET  | `/agent/export` | 返回 `phone_samples.csv / phone_events.csv / phone_session.json / phone_sync.json` |
| POST | `/agent/session` | 下发 experiment/run plan（仅存储） |
| POST | `/agent/arm` | ARMED + 离线倒计时（直接 ARM 绕过对时，仅调试用） |
| POST | `/agent/abort` | 中止 |
| POST | `/agent/downlink` | 下行探测；手机调用 `recordDownlinkAck()` 记录后回上行 ACK（`phoneRecvMs`/`phoneSendMs`/`phoneElapsedNs`） |
| **POST** | **`/agent/sync/confirm`** | **对时确认**：`pc_timestamp_ms + gnb_data_timestamp_ms + plan` → 写 SyncAnchor + SYNC_CONFIRM marker → `RunEngine.arm(plan)`（对时后才 arm，主路径） |
| GET/POST | `/agent/tasks` | 实验列表 / 下发任务（含 purpose/flow/templates） |
| POST | `/agent/task/start` | 开始实验 → 发送 `ACTION_START_SERVICE` → 环境监测模式（等 PC sync-confirm） |
| POST | `/agent/task/stop` | 停止实验 → 记录 `PHONE_STOP` marker（`stopUtcMs + stopElapsedNs`） |
| POST | `/agent/collected` | 记录采集（时间 + 主机名 + 已采集次数） |

## 关键实现约束

- 双时间轴：每个 sample 同时记录 `utc_epoch_ms` 与 `elapsed_realtime_ns`。
- 离线执行：倒计时与 phase transition 全部用 monotonic clock（`SystemClock.elapsedRealtimeNanos()`）。
- **对时主流程**：手机先 `ACTION_START_SERVICE`（只监测不 ARM）→ 收 PC downlink 回 ACK →
  PC 收 ACK 后发 `/agent/sync/confirm` → 手机 `handleSyncConfirm()` 写锚点后才 `arm(plan)` →
  进入 baseline→active→tail。**严禁在未收到 sync-confirm 时直接进入正式采集。**
- Phase / Sync Markers：`RUN_ARMED / BASELINE_START / BASELINE_END / ACTIVE_START / ACTIVE_END /
  TAIL_START / TAIL_END / RUN_COMPLETE / RUN_ABORTED / SYNC_CONFIRM / SYNC_CONFIRM_DUP /
  DOWNLINK_ACK / PHONE_STOP / AIRPLANE_MODE_ON / AIRPLANE_MODE_OFF / SERVING_CELL_CHANGED`。
- 采集：battery（`CURRENT_NOW/AVERAGE/CHARGE_COUNTER` 保留 OEM 符号）、voltage、SOC、温度；
  NR（SS-RSRP/RSRQ/SINR，设备支持时 CSI-*；PCI/NCI/TAC/NRARFCN）；thermal（system status + headroom）；
  confounder（screen/plugged/charging/Wi-Fi/BT/airplane）；
  workload（`workload_type / workload_target_mbps / workload_actual_mbps`）。
- **不支持 → null，绝不写 0**；派生 `battery_power_w = |I|·V` 不替代原始值。
- 不采集 gNB 特权量（TPC/PH/PRB/MCS/HARQ 来自 OAI）。
- 手机侧无 RSSI（Android NR 公共 API 不提供与 gNB PUSCH RSSI 等价的量）。

## 5G 通道（无 USB 对时）

AgentServer 绑定 `0.0.0.0:8420`，因此既能被 `adb forward`（USB）访问，也能经手机 PDU IP
（`10.0.1.x`/`10.0.0.x`）从 PC 直连（需按 `../experiment_platform/README.md` 配好路由）。

## 大流量工作负载

- `UL_CBR`：UDP 打流到指定地址（默认 `192.168.70.129:5201`），`Network.bindSocket` 强制走蜂窝。
- `DL_SATURATION`：循环下载阿里云镜像（上海节点，离苏州近）：
  `https://mirrors.aliyun.com/ubuntu-releases/24.04/ubuntu-24.04.1-desktop-amd64.iso`，
  `Network.openConnection` 强制走蜂窝。
- 字节计数用 `TrafficStats`（app UID TX/RX）核验，并记录 `workload_target_mbps / workload_actual_mbps`。
