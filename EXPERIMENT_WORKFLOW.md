# EXPERIMENT_WORKFLOW.md — 实验工作流与状态机

> 本文档定义一次正式 run 的完整操作时序、状态机转移、对时算法与融合步骤。
> 核心时序（任务书 §1）：
> `Pre-sync → Preload experiment → Offline execution → Reconnect → Post-sync → Data import`

## 1. 状态机

```text
DRAFT ──► PREPARING ──► WAITING_GNB ──► SYNCING_PHONE ──► ARMED ──► PHONE_OFFLINE
──► RUNNING ──► WAITING_PHONE_RETURN ──► IMPORTING ──► ALIGNING ──► COMPLETE
                                                                       ├──► WARNING
                                                                       └──► FAILED
```

每次 transition 写 `run_transitions`（run_id, from, to, utc_ms, note）。
程序重启后按 `experiment_id + run_id` 读库恢复当前状态，绝不重复执行已完成步骤。

## 2. Prepare Run 工作流（对应任务书 §33，15 步）

| Step | 动作 | 对应 OAI 调用 |
|------|------|---------------|
| 1 | 验证 OAI | `GET /api/health` |
| 2 | 读 pre-state | `GET /api/status`、`/api/gnb/controls`、`/api/research/config`（存 before） |
| 3 | 应用本 condition OAI config | 依次 POST（见 §4 多参数合并重启） |
| 4 | 若重启 | 等待 `GET /api/gnb/progress?id=…` 完成 |
| 5 | 验证就绪 | gNB running、UE in-sync、`collection.stale==false`、`phRawDb` 可得、TPC events 活跃（适当条件） |
| 6 | USB 对手机 pre-sync | 10–20 次 time exchange（§3） |
| 7 | 下发 experiment/run plan | `POST /agent/session` + `POST /agent/arm` |
| 8 | 手机 ARMED | PC 显示 “Disconnect USB / Place DUT” |
| 9 | 双方本地倒计时 | 手机按 `elapsedRealtimeNanos` 倒计时；PC 显示预计开始时间 |
| 10 | PC 开始连续记录 | snapshot collector 1 Hz + event collector + config |
| 11 | 手机离线完成 | baseline → active → tail |
| 12 | run 完成 | 重连 USB |
| 13 | post-sync | 再执行 time exchange |
| 14 | 自动导入手机数据 | `GET /agent/export` 或 `adb pull` |
| 15 | 时间修正 + 对齐 + merge + quality checks | §5 |

## 3. 对时算法（不能只执行一次）

1. 实验前执行 **≥10–20 次** time exchange。
2. 每次记录：`t1=PC send`、`t2=phone receive/reply timestamp`、`t3=PC receive`。
3. `RTT = t3 - t1`；`offset ≈ t2 - (t1 + RTT/2)`（单程估计）。
4. 取**最低 RTT 的若干样本**（默认最低 30% 或 ≥5 个）做 offset 的 median/MAD，得
   `sync_offset_ms`、`sync_uncertainty_ms`（用 MAD 或 min-RTT 样本的 std）。
5. 存 `sync_before.json`；实验后重连再存 `sync_after.json`。
6. 由 before/after 估计 `clock offset before/after` 与 `clock drift`（线性插值）。

数据库必存：`sync_rtt_ms, sync_offset_ms, sync_uncertainty_ms`（不能说“已经对时”）。

## 4. 多参数同时变化时的合并重启（任务书 §11）

当 condition 同时改变 bandwidth / TX gain / PUSCH target / MCS/Qm/PRB：

1. 各字段先以 `"restart":false` 持久化（POST bandwidth/gains/pusch-target-snr/ul-scheduler）。
2. 所有参数完成。
3. 最后一次统一 `POST /api/gnb/service {"action":"restart"}`（或让最后一步带 `restart:true`）。
4. 用 progress API 等待完成。
5. 重新读取 `/api/research/config` 与 `/api/gnb/controls`。
6. 校验 actual condition（requested_config vs actual_config）。
7. 确认 UE in-sync。
8. 再开始 experiment。

分析只依据 `actual_config`。若 before/after 关键实验变量不一致 → `quality_flag=CONFIG_CHANGED`。

## 5. 数据融合步骤（Level 0/1/2）

1. **Level 0**：手机 raw 与 OAI raw 原样落盘，记录 SHA-256（`files` 表）。
2. **Level 1**：对手机 `utc_epoch_ms` 施加线性 drift 修正
   `t_corrected = t_phone - offset(t)`，`offset(t)` 由 pre/post anchor 线性插值。
   OAI 侧用 API/event 自带 `timestampEpochNs`（不需要修正）。
3. **Level 2**：以 `t_corrected` 秒为单位做 1 s window：
   - 手机：mean/median/std current、mean voltage、mean power、integrated energy（`∫P dt`）、
     RSRP median/p10/p90、SINR median、temperature。
   - gNB snapshot：该窗口最近且 `collection.stale==false` 的有效 snapshot。
   - gNB events：event_count、TPC +/0/- count、TPC positive ratio、PH raw/normalized mean、
     PCMAX、PUSCH SNR mean、RSSI mean、MCS mean/mode、N_PRB mean、Qm mode、HARQ delta、
     retransmission ratio、BLER、DTX。
4. 派生 energy：`baseline/active/tail/total_energy_J = ∫P dt`（分 phase）；
   有字节时 `E_bit = (E_active - E_baseline_equivalent)/successfully_delivered_bits` 及 `J/MB`。
   **原始 current/voltage/power/bytes 全部保留**。

## 6. OAI 采集后台

- **Snapshot collector**：约 1 Hz 轮询 `/api/research/ues`，保存完整 raw JSON + normalized 表。
- **Event collector**：轮询 `/api/research/events`（频率足够避免 server 端 recent buffer 被覆盖，但不 busy loop），
  去重键 `timestampEpochNs + rnti + frame + slot`（写库 UNIQUE，幂等），实现 retry/backoff/API error logging。
- **Config provenance**：run 开始读 status/controls/config，run 结束再读，存 before/after。
- 每个 run 独立 collector；失败可恢复（按 run_id 续采，不重复）。

## 7. 手机离线执行（App 内部）

- 计划下发后 `ARMED`，USB 断开。
- App 用 `elapsedRealtimeNanos()` 做倒计时与 phase transition（不依赖 wall clock）。
- phase 变化写 marker：`RUN_ARMED, BASELINE_START, ACTIVE_START, ACTIVE_END, TAIL_END, RUN_COMPLETE`。
- 正式窗口内：**不绘图、不轮询 OAI、不轮询 PC、不云上传、不 Wi-Fi 同步、不写远端库**。
- 只采样 + 本地存储 + 可选 workload。

## 8. 手机↔PC 控制通道

优先 USB-only 本地 HTTP：
1. App 在连接状态下只监听手机本地 `127.0.0.1:<port>` 的轻量控制服务。
2. PC 用 `adb forward tcp:<pc_port> tcp:<phone_port>` 建立 USB-only tunnel。
3. 端点：`GET /agent/status`、`GET /agent/time`、`POST /agent/session`、`POST /agent/arm`、
   `POST /agent/abort`、`GET /agent/export`。
4. 退化方案：ADB broadcast + JSON plan file + `adb pull`（导入流程相同）。

## 9. 质量检查（run 结束自动执行）

检查项与标志见 `DATA_SCHEMA.md §5.2`。输出 `quality_status ∈ {PASS,WARNING,FAILED}` + `quality_flags[]`。
**只标记，不删除问题 run**。

## 10. 验收 dry run

见 `SYSTEM_DESIGN.md §11`。真实硬件不在线时使用 **明确标记 MOCK/TEST** 的合成夹具，
测试数据不得冒充真实实验数据。
