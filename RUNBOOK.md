# RUNBOOK.md — 正式 dry run 操作手册

> 目标：按任务书 §62 完成一次端到端 dry run。
> 前置：手机已装 `android/` 的 5G Energy Experiment Agent；手机 USB 连到本机；
> OAI 控制中心在 LAN `192.168.31.119:8787` 可达且 gNB/核心网已启动。

## 0. 启动平台

```powershell
cd E:\Pythonprojects\MOBILE\experiment_platform\web
npm install; npm run build          # 仅首次 / 前端改动后
cd ..
python -m experiment_platform.backend.server
# 打开 http://127.0.0.1:8900
```

## 1. 确认 OAI 与手机

```powershell
# OAI 健康（网页“Settings”可改 host/port/token）
curl http://192.168.31.119:8787/api/health

# 手机
adb devices          # 应显示 53616213  device
```

## 2. 建立 USB-only 控制通道（App 端 loopback server + adb forward）

```powershell
adb -s 53616213 forward tcp:8420 tcp:8420
# 验证
curl http://127.0.0.1:8420/agent/status
```

## 3. 平台建实验/条件/run

- 网页 **Experiment**：新建 `AC_20260815_A`（environment=AC）。
- 网页 **Run Planner**：建 condition（40 MHz、PUSCH target manual 15 dB、UL scheduler AUTO、
  业务 UL_CBR、orientation/chamber metadata），再建 run。

## 4. Prepare Run（网页按钮）

平台依次执行：`/api/health` → 存 before → 应用配置（40 MHz → Target SNR → UL AUTO，
合并为一次 restart）→ `progress` 等待 → 读 actual config → 校验 gNB running / UE in-sync /
`collection.stale==false`。

## 5. 对时 + 下发计划 + ARM

- 网页点击 **Sync**（10–20 次 time exchange，显示 offset/rtt/uncertainty）。
- 网页点击 **Arm**（下发 plan + `POST /agent/arm`）。
- 手机显示 ARMED；网页显示 **Disconnect USB / Place DUT**。

## 6. 离线运行（断开 USB）

- 拔掉 USB。手机按 `elapsedRealtimeNanos` 倒计时，执行 baseline 30 s → active 120 s → tail 60 s，
  写 marker：RUN_ARMED / BASELINE_START / ACTIVE_START / ACTIVE_END / TAIL_END / RUN_COMPLETE。
- 全程屏幕关、Wi-Fi 关、无充电；只本地采样 + 本地存储 + 可选 workload。

## 7. PC 独立记录 gNB

- 网页点击 **Start Collectors**：1 Hz `/api/research/ues` + `/api/research/events` 持续落盘（raw JSON + 归一化表）。

## 8. 重连 USB + Post-sync + 导入 + 融合

- 重插 USB，重建 `adb forward`。
- 网页点击 **Stop Collectors**（存 config after）。
- 网页点击 **Post-sync**（再对时，得 drift）。
- 网页点击 **Import**（`GET /agent/export` 拉取 phone_samples.csv 等，存 `raw/phone/<run_id>/`）。
- 网页点击 **Align**（时钟修正 + `merged_1s.csv`）。
- 网页点击 **Quality**（`quality_status` + flags，只标记不删数据）。

## 9. 验收查看与导出

- **Run Detail**：同一时间轴看到 Phone(battery current/power, SS-RSRP, SS-SINR, temperature) +
  gNB(RSRP, PUSCH SNR, TPC, PH raw/normalized, PCMAX, MCS/Qm/N_PRB, HARQ/BLER) + phase 阴影。
- **Export Experiment**：下载 `experiment_AC_20260815_A.zip`（manifest + raw + processed + features_m1/m2/m3 + parquet）。

## 10. 无硬件时的替代验证

```powershell
python -m experiment_platform.backend.seed_mock   # 生成明确标记 MOCK 的合成 dry run
python -m pytest experiment_platform/backend/tests -q
```

**注意**：合成数据只能用于验证流程，绝不能作为论文实验数据。
