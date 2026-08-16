# 5G Smartphone Energy Experiment System — 系统设计

> 版本：0.1（初稿，基于 2026-08-15 对 OAI 控制中心 `:8787` 的真实 API 探测）
> 目标研究链条：
> `Physical RF condition → UE-observable state → gNB power-control/scheduling state → smartphone electrical energy`

## 1. 文档目的与依据

本文档是开发依据，**不是**凭任务书猜测，而是基于当天对真实 OAI API 的逐字段探测结果写成。
真实 API 的 JSON 样本保存在 `data/oai_schema/*.json`，是实现 OAI 客户端与数据模型的唯一事实来源。
任务书中描述的字段名与实际返回字段名存在多处差异（例如任务书写 `RSRP`/`PCMAX`，实际为 `rsrpDbm`/`pcmaxDbm`），
本系统的数据模型一律以 `data/oai_schema/` 为准，任务书仅作为需求清单。

## 2. 系统边界与最重要约束

**正式实验窗口内，手机与实验平台没有任何连接：**

- USB 数据线断开，不依赖 ADB；
- 不依赖 Wi-Fi；
- 不依赖实验控制平台 HTTP/WebSocket；
- 手机不轮询 OAI API；
- 所有手机数据本地记录；
- 测试结束后重新连接手机，再把数据传回平台。

因此系统采用固定时序：

```text
Pre-sync → Preload experiment → Offline execution → Reconnect → Post-sync → Data import
```

**禁止**：PC 每秒控制手机 / 手机每秒上传 / 手机实时读 gNB。

手机与 OAI 之间**不直接交换实验控制数据**。PC 平台是唯一的数据融合中心。

## 3. 三个独立数据域

```text
┌─────────────────────────────┐       USB/ADB only before/after
│ Android Experiment Agent    │◄──────────────────────────────┐
│ battery / NR / thermal /    │                               │
│ workload / local timestamps │                               │
│ offline Room(SQLite)        │                               │
└──────────────┬──────────────┘                               │
               │ USB/ADB (仅实验前后)                          │
               ▼                                               │
┌─────────────────────────────┐       LAN HTTP :8787          │
│ PC Experiment Platform      │──────────────────────────────►┼──┐
│ experiment/run/condition    │                               │  │
│ OAI client + collectors     │◄──────────────────────────────┼──┘
│ time alignment + fusion     │                               │
│ visualization + export      │                               │
└─────────────────────────────┘                               │
                                                              ▼
                                             ┌─────────────────────────────┐
                                             │ Existing OAI Control Center │
                                             │ :8787 REST (LAN)            │
                                             │ gNB control / telemetry /   │
                                             │ research snapshot & events  │
                                             └─────────────────────────────┘
```

## 4. 组件清单

| # | 组件 | 技术 | 部署位置 |
|---|------|------|----------|
| 1 | Android Experiment Agent | Kotlin + Jetpack Compose + Foreground Service + Room/SQLite | 测试手机 |
| 2 | PC 实验控制平台后端 | Python 3.14 + FastAPI + SQLite（文件系统存 raw） | 本机（Windows） |
| 3 | OAI 控制客户端 | 平台后端内 `oai_client.py` | 本机 → LAN |
| 4 | 时间同步/融合 | 平台后端内 `sync.py` / `fusion.py` | 本机 |
| 5 | 实验网站 | React 19 + TypeScript + Vite（构建产物由后端托管） | 本机 |
| 6 | 导出系统 | 平台后端内 `export.py`（ZIP + manifest + CSV/Parquet） | 本机 |

## 5. 已确认的 OAI API 事实（探测日期 2026-08-15）

### 5.1 连接配置

- 探测地址：`http://192.168.31.119:8787`（与 `03-可视化网站搭建与修改日志.md` 记录一致）。
- **本机处于同一网段** `192.168.31.27`，可路由。
- **LAN 写控制未被拦截**：POST 从 LAN 发回的是业务校验错误（HTTP 400 中文错误），而非旧日志记录的 HTTP 403。
  说明当前 OAI 控制中心以 `OAI_LAN_CONTROL=1` 或等价方式运行。
- **当前未强制 X-OAI-Control-Token**：无 token 与带 dummy token 的 POST 均到达业务校验层。
  平台仍按任务书要求预留可选 `X-OAI-Control-Token` 头支持，token 通过本地 secret 配置注入，不写死、不入库、不导出。
- gNB 探测时处于 **stopped**（`status.gnb.running=false`），核心网 10/10 运行。
- 探测时 `research/config`：`bandwidthMHz=100, frequencyMHz=3349.92, txGainDb=60, rxGainDb=40, puschTargetMode=manual, puschTargetSnrX10=89(8.9dB), ulSchedulerMode=auto`。

### 5.2 已存在且已验证的 GET 端点

| 端点 | 用途 |
|------|------|
| `GET /api/health` | `{"ok":true}` 仅表示 API 进程正常 |
| `GET /api/status` | gNB/5GC/radio/ues/hardware/communication 聚合 |
| `GET /api/gnb/controls` | UL scheduler + PUSCH target + observedUplink |
| `GET /api/research/ues` | **1 Hz gNB snapshot**（UE PHY/MAC 全量，见 §5.3） |
| `GET /api/research/config` | 真实 gNB 配置 + 嵌套 controls |
| `GET /api/research/events?limit=N` | PUSCH/TPC 事件流 |
| `GET /api/rf/calibration` | TX gain ↔ measured dBm 标定 |
| `GET /api/gnb/progress?id=<requestId>` | 控制操作进度（无 id 返回最近一次操作） |

### 5.3 `/api/research/ues` 实际字段（权威 schema，见 `research_ues.json`）

顶层：`timestampUtc, timestampEpochNs, timestampMonotonicNs, experimentId, source, collection{available,error,samplingHz,rssiUnit,latestAgeSeconds,stale}, ues[]`

每个 UE：
```text
rnti, cuId, state, timestampUtc, timestampEpochNs, updatedAtUtc, ageSeconds
rsrpDbm, ssbSinrDb
downlink{mcs, mcsTable, qm, bler, harqRounds[4], harqErrors, harqInitialTxDelta,
         harqRetransmissionDelta, harqRetransmissionRatio, dtx, dtxDelta,
         goodputMbps, cqi, ri, pmi}
uplink{mcs, mcsTable, qm, bler, harqRounds[4], harqErrors, harqInitialTxDelta,
       harqRetransmissionDelta, harqRetransmissionRatio, dtx, dtxDelta,
       goodputMbps, ulRi, tpmi, nPrb, puschSnrDb, puschRssi, puschRssiUnit}
powerControl{phCeCode, phRawDb, phNormalizedDb, pcmaxCeCode, pcmaxDbm,
             pcmaxMinusRawPhDb, puschTargetSnrX10, puschTargetSnrDb,
             tpcPusch, tpcInFlightDb, deltaMcsDb, updatedAtUtc, ageSeconds,
             phRawUpdatedAtUtc, phRawAgeSeconds}
imsi, timestampMonotonicNs
```

要点（与任务书措辞的对应）：
- `rsrpDbm` = RSRP；`ssbSinrDb` = SSB SINR（可 null）。
- `pcmaxDbm`/`pcmaxCeCode` = PCMAX；`phRawDb`/`phCeCode`/`phNormalizedDb` = PH。
- `uplink.puschSnrDb` = PUSCH SNR；`uplink.puschRssi` + `uplink.puschRssiUnit` = PUSCH RSSI。
- **RSSI 单位为 `dBFS`**（`collection.rssiUnit` 与 `puschRssiUnit` 均实测为 `"dBFS"`），不是 dBm。
- HARQ counters = `harqRounds[]` + `harqErrors`；HARQ delta = `harqInitialTxDelta`/`harqRetransmissionDelta`；retransmission ratio = `harqRetransmissionRatio`；DTX = `dtx`/`dtxDelta`。
- `downlink.qm`、`downlink.cqi/ri/pmi`、`uplink.ulRi/tpmi` 可为 null（设备/数据是否上报而定）。

### 5.4 `/api/research/events` 实际字段

顶层：`timestampUtc, timestampEpochNs, timestampMonotonicNs, limit, count, events[]`
每个 event：
```text
timestampUtc, timestampEpochNs, timestampMonotonicNs, rnti, frame, slot,
puschSnrDb, phNormalizedDb, tpcPusch, tbSizeBytes, tpcInFlightDb,
deltaMcsDb, nPrb, mcs, rssi, rssiUnit
```
去重键：`timestampEpochNs`（事件级纳秒唯一）+ `rnti` + `frame` + `slot`（稳定唯一键，如任务书要求）。

### 5.5 `/api/research/config` 实际字段

```text
puschTargetSnrX10, pucchTargetSnrX10, ulschMaxFrameInactivity,
ulBlerTargetUpper/Lower, ulMinMcs/ulMaxMcs, dlBlerTargetUpper/Lower, dlMinMcs/dlMaxMcs,
minGrantPrb, deltaMcsEnabled, ulManualMcs, ulManualPrb, puschTargetSnrDb,
timestampUtc, timestampEpochNs, timestampMonotonicNs, configPath, available, error,
frequencyMHz, bandwidthMHz, txGainDb, rxGainDb, ulSchedulerMode, puschTargetMode,
controls{…同 /api/gnb/controls…}
```
**null 语义**：`ulBlerTargetUpper/Lower`、`ulMinMcs/ulMaxMcs`、`dlBlerTargetUpper/Lower`、`dlMinMcs/dlMaxMcs`、`minGrantPrb`、`deltaMcsEnabled` 在未配置时为 `null`（探测值即 null），平台**保存 null，不补 0/默认值**。

### 5.6 POST 端点与校验（实测）

| POST | 合法值/校验 | 实测成功响应关键字段 |
|------|-------------|----------------------|
| `/api/gnb/service` | `action ∈ {start,stop,restart}` | `ok`；触发重启时含 `requestId`（格式 `service-<epochMs>`） |
| `/api/gnb/bandwidth` | `bandwidthMHz ∈ {40,100}` | `ok,message,bandwidth{previousBandwidthMHz,bandwidthMHz,carrierPrb,bwpRiv,coresetZero,pointA,frequencyMHz,changed},restarted` |
| `/api/gnb/frequency` | n78 范围，后端做 1.44 MHz raster 对齐 | `ok,restarted`（前端不重复实现对齐） |
| `/api/gnb/gains` | `txGainDb,rxGainDb` 0–60 | `ok,restarted` |
| `/api/gnb/pusch-target-snr` | `mode ∈ {auto,manual}`，manual 时 `puschTargetSnrX10` | `ok,message,target{mode,previousMode,previousX10,previousDb,targetX10,targetDb,autoTargetX10,autoTargetDb,changed,effectiveChanged},restarted` |
| `/api/gnb/ul-scheduler` | `mode ∈ {auto,manual}`，manual 时 `mcs,qm,prb` | `ok,message,policy{mode,mcs,qm,prb,mcsTable,previous,changed},restarted` |

- 无重启需求时 `restarted=false` 且**无 `requestId`**；需要重启时平台应等待 `requestId` 并通过 progress 轮询。
- **不存在** `/api/gnb/coding-rate`，编码方式只能由 MCS/Qm/N_PRB 体现，平台据此记录理论 code-rate metadata。

### 5.7 `/api/gnb/progress` 语义（实测）

```json
{"requestId":"service-1786785740917","active":false,"action":"stop",
 "phase":"complete","message":"OAI 服务已恢复","progress":100,"error":"","updatedAt":"..."}
```
- 无 `id` 参数：返回**最近一次**操作状态。
- 带 `id`：返回该请求状态；未知 id 回退为 `{"active":true,"action":"restart","phase":"queued","progress":4,...}` 模板。
- 已观测 phase：`queued`(progress 4) → … → `complete`(progress 100)。中间 phase 值未在探测中枚举，
  平台采用**宽容轮询**：直到 `active==false && phase∈{complete,done,ready,finished}` 判定完成；`error!=""` 判定失败；超时判定 WARNING。

### 5.8 `/api/status` 关键字段（额外有用）

- `radio.supportedBandwidthMHz=[40,100]`、`radio.carrierPrb`、`radio.centerFrequencyMinMHz/MaxMHz`。
- `radio.transport.supports100MHz`（10GbE/PCIe 满足 100 MHz 前提）。
- `gnb.running`、`core.running/total`、`ues[]`（in-sync UE，字段与旧日志一致：rnti/cuId/state/rsrp/snr/dlMbps/ulMbps/dlMcs/ulMcs/dlBler/ulBler/imsi）。
- `hardware.x410.present`（表示 gNB 容器运行，非独立 UHD 探测）、`hardware.scope.active`。

## 6. 技术栈决策（已按本机环境核验）

| 层 | 决策 | 理由 |
|----|------|------|
| Android | Kotlin + minSdk 29 + Jetpack Compose + Room(KSP) + Foreground Service | 本机 Android SDK（platform 36 / build-tools 35·36）、Java 18、Gradle 缓存齐备，可构建 |
| 后端 | Python 3.14 + FastAPI + uvicorn + pydantic + httpx + SQLite + pandas + pyarrow | 本机 Python 3.14、PyPI 可达；requests/pandas/numpy 已装，其余可安装 |
| 前端 | React 19 + TypeScript + Vite | 与 OAI 控制中心工程风格一致；npm registry 可达 |
| 存储 | SQLite=元数据/索引；文件系统=raw 实验文件；每文件记录 SHA-256 | 任务书 §51，避免大块 JSON 塞进 DB |
| 手机↔PC | 本地 HTTP（`127.0.0.1` + `adb forward`）为主，退化方案 ADB broadcast + plan JSON + `adb pull` | 任务书 §15 |

## 7. 数据分级（Level 0/1/2）

- **Level 0 raw**：`raw/phone/`（phone_samples.csv、phone_events.csv、phone_session.json、phone_sync.json）
  `raw/oai/`（snapshots raw JSON、events raw JSON、config before/after）。**永不覆盖**。
- **Level 1 clock-corrected**：`processed/time_aligned/`，用 pre/post sync anchor 做线性 drift 修正后的带 `t_corrected_epoch_ms` 的行。
- **Level 2 merged 1s**：`processed/merged_1s.csv`，统一 1 s 分析窗口。

## 8. 科研硬性原则（贯穿实现）

```text
raw data never overwritten
requested condition ≠ actual condition
phone clock ≠ PC clock
missing ≠ 0
Android public telemetry ≠ gNB privileged telemetry
phone RSRP ≠ physical incident power density
```

- 保存 `requested_config` 与 `actual_config` 两套；分析只依据 `actual_config`。
- 手机侧**禁止**采集 TPC/PH/PRB/MCS/HARQ（属于 gNB privileged 域），保证 `X_UE` / `X_gNB` 权限边界。
- 手机 RSSI：Android NR 公共 API 不保证提供与 gNB `PUSCH RSSI` 等价的量，`phone_rssi_dbm=null`；gNB 的存为 `gnb_pusch_rssi`/`gnb_pusch_rssi_unit`。

## 9. 失败恢复与状态机

Run 状态机（每次 transition 写库，可按 experiment_id+run_id 恢复）：
```text
DRAFT → PREPARING → WAITING_GNB → SYNCING_PHONE → ARMED → PHONE_OFFLINE
      → RUNNING → WAITING_PHONE_RETURN → IMPORTING → ALIGNING → COMPLETE|WARNING|FAILED
```

## 10. 开发顺序（任务书 §60）

```text
Phase A Android raw logger → Phase B phone/PC sync → Phase C OAI client(只读→POST)
→ Phase D experiment state machine → Phase E visualization
```

## 11. 验收 dry run 目标

端到端（真实硬件不在线时用 **明确标记 MOCK/TEST** 的合成夹具代替）：
Pre-sync → 建 AC run → OAI 40 MHz → 设 Target SNR → UL scheduler AUTO → 等 gNB ready → UE in-sync
→ Arm phone → 离线 30+120+60 s → PC 独立记录 gNB → 重连 → Post-sync → 导入 → 时钟修正 → merged_1s.csv
→ 网页对齐时间轴 → 导出完整 ZIP。
