# DATA_SCHEMA.md — 数据模型与列定义

> 版本：`schema_version = 1`（字段变更时必须递增，并写入 manifest.json）
> 本文件是平台、Android App、导出文件三者的唯一列定义来源。字段名一经发布不可随意改名；
> 确需改名时递增 `schema_version` 并在 manifest 中记录。

## 0. 通用约定

- 所有行必带上下文键：`experiment_id, session_id, run_id, condition_id, device_id`（避免按文件名猜归属）。
- 时间双轴：`utc_epoch_ms`（合并用）+ `elapsed_realtime_ns`（手机内顺序/积分/时长）。
- **missing ≠ 0**：不支持/不可得的数值一律 `null`（空），绝不写 `0`。
- 所有 OAI 原始 JSON 原样保存（Level 0），不因“前端只显示部分字段”而丢弃字段。

---

## 1. Android App 本地数据库（Room 实体）

### 1.1 DeviceInfo
`device_id(PK), manufacturer, model, codename, android_version, sdk_int, build_fingerprint, app_version, battery_health, battery_capacity_uah`

### 1.2 Experiment
`experiment_id(PK), environment(AC|RC), operator_name, notes, created_utc_ms`

### 1.3 Session
`session_id(PK), experiment_id(FK), device_id(FK), started_utc_ms, ended_utc_ms`

### 1.4 Condition
`condition_id(PK), experiment_id(FK), environment, orientation_deg, incident_power_density_wm2, stirrer_state, target_rsrp_dbm, traffic_condition, frequency_mhz, bandwidth_mhz, tx_gain_db, rx_gain_db, pusch_target_snr_db, pusch_target_snr_x10, pusch_target_mode, scheduler_mode, mcs, qm, n_prb, chamber_metadata_json`

### 1.5 Run
`run_id(PK), experiment_id, session_id, condition_id, device_id, planned_start_utc_ms, start_delay_s, state, started_elapsed_realtime_ns, ended_elapsed_realtime_ns, random_seed, planned_order, actual_order`

### 1.6 Phase / 阶段（内嵌到 Sample 的 phase 字段 + EventMarker）
phase 取值：`ARMED, BASELINE, ACTIVE, TAIL, COMPLETE`（子标记见 §5）

### 1.7 PhoneSample（核心行，§2 的 CSV 即此实体的导出）
`id(PK,autoincrement), utc_epoch_ms, elapsed_realtime_ns, experiment_id, run_id, condition_id, phase, + §2 全部遥测列`

### 1.8 SyncAnchor
`id(PK), direction(before|after), attempt_index, t1_pc_send_ms, t2_phone_recv_elapsed_ns, t2_phone_utc_ms, t3_pc_recv_ms, rtt_ms, offset_ms, uncertainty_ms`

### 1.9 EventMarker
`id(PK), utc_epoch_ms, elapsed_realtime_ns, experiment_id, run_id, condition_id, marker_type`
marker_type 取值：`RUN_ARMED, BASELINE_START, ACTIVE_START, ACTIVE_END, TAIL_END, RUN_COMPLETE, SERVING_CELL_CHANGED, RUN_ABORTED, …`

---

## 2. 手机导出文件

### 2.1 `phone_samples.csv`（每行一个采样）

```text
utc_epoch_ms, elapsed_realtime_ns,
experiment_id, run_id, condition_id, session_id, device_id, phase,
battery_current_now_ua, battery_current_average_ua, battery_voltage_mv, battery_power_w,
charge_counter_uah, soc_percent, battery_temperature_c, thermal_status, thermal_headroom,
ss_rsrp_dbm, ss_rsrq_db, ss_sinr_db,
csi_rsrp_dbm, csi_rsrq_db, csi_sinr_db, csi_cqi,
nrarfcn, pci, nci, tac, network_type,
screen_state, plugged, charging, wifi_state, bluetooth_state, airplane_mode,
workload_type, workload_target_mbps, workload_actual_mbps, app_tx_bytes, app_rx_bytes,
sample_quality_flags
```

字段语义：
- `battery_power_w`：`|I|·V` 派生值（**派生不替代原始 current/voltage**）；`null` 当电流/电压缺失。
- `battery_current_now_ua`/`battery_current_average_ua`：OEM 原始符号保留；unsupported=null。
- `thermal_status`：Android system thermal status（0..6）；`thermal_headroom` 不支持则 null。
- `csi_*`/`csi_cqi`：设备支持才非空，否则 null。
- `sample_quality_flags`：`;` 分隔（如 `USB_CONNECTED;WIFI_ON`）。
- 无手机侧 RSSI 列——Android NR 公共 API 不保证提供与 gNB PUSCH RSSI 等价的量。

### 2.2 `phone_events.csv`
```text
utc_epoch_ms, elapsed_realtime_ns, experiment_id, run_id, condition_id, marker_type, payload_json
```

### 2.3 `phone_session.json`
DeviceInfo + Experiment + Session + Condition + Run 元数据（含 chamber metadata、requested sampling rate）。

### 2.4 `phone_sync.json`
```json
{"direction":"before|after", "attempts":[{"t1_ms":..., "t2_elapsed_ns":..., "t2_utc_ms":..., "t3_ms":...}],
 "rtt_ms_min":..., "offset_ms":..., "uncertainty_ms":..., "n_samples":...}
```

---

## 3. PC 平台 SQLite（元数据/索引）

| 表 | 关键列 |
|----|--------|
| `experiments` | experiment_id(PK), environment, operator_name, notes, created_utc, schema_version |
| `devices` | device_id(PK), manufacturer, model, codename, android_version, sdk_int, build_fingerprint, app_version, battery_health, battery_capacity_uah |
| `conditions` | condition_id(PK), experiment_id, environment, orientation_deg, incident_power_density_wm2, stirrer_mode, stirrer_state, target_rsrp_dbm, traffic_condition, frequency_mhz, bandwidth_mhz, tx_gain_db, rx_gain_db, pusch_target_snr_db, pusch_target_snr_x10, pusch_target_mode, scheduler_mode, mcs, qm, n_prb, chamber_metadata_json |
| `runs` | run_id(PK), experiment_id, condition_id, device_id, session_id, state, planned_order, actual_order, random_seed, planned_start_utc_ms, start_delay_s, requested_config_json, actual_config_json, started_utc_ms, ended_utc_ms, quality_status, quality_flags_json |
| `sync_anchors` | run_id, direction, attempt_index, t1_ms, t2_elapsed_ns, t2_utc_ms, t3_ms, rtt_ms, offset_ms, uncertainty_ms |
| `oai_snapshots` | id, run_id, fetched_utc_ms, ts_epoch_ns, ts_utc, rnti, imsi, rsrp_dbm, ssb_sinr_db, ph_raw_db, ph_normalized_db, pcmax_dbm, pusch_snr_db, pusch_rssi, pusch_rssi_unit, ul_mcs, dl_mcs, qm, n_prb, cqi, ri, pmi, ul_ri, tpmi, ul_bler, dl_bler, harq_*, dtx, goodput_mbps, collection_stale, raw_json_path |
| `oai_events` | id, run_id, ts_epoch_ns, ts_utc, rnti, frame, slot, pusch_snr_db, ph_normalized_db, tpc_pusch, tb_size_bytes, tpc_in_flight_db, delta_mcs_db, n_prb, mcs, rssi, rssi_unit, dedup_key(UNIQUE), raw_json_path |
| `oai_config` | id, run_id, stage(before|after), config_json_path, sha256 |
| `files` | file_path, size_bytes, sha256, created_utc |
| `run_transitions` | run_id, from_state, to_state, utc_ms, note |
| `phone_samples`（导入后） | 与 §2.1 同列 + `t_corrected_epoch_ms` |

---

## 4. OAI 采集导出（Level 0 raw）

```
raw/oai/snapshots/<run_id>__<ts_epoch_ns>.json      # /api/research/ues 完整原样
raw/oai/events/<run_id>__<ts_epoch_ns>.json          # /api/research/events 完整原样
raw/oai/config/gnb_config_before.json
raw/oai/config/gnb_config_after.json
raw/oai/status/gnb_status_before.json
raw/oai/status/gnb_status_after.json
raw/oai/controls/gnb_controls_before.json
raw/oai/controls/gnb_controls_after.json
```

---

## 5. 事件标记（marker_type）与质量标记（quality flag）全集

### 5.1 Phase marker
`RUN_ARMED, BASELINE_START, ACTIVE_START, ACTIVE_END, TAIL_END, RUN_COMPLETE, RUN_ABORTED, SERVING_CELL_CHANGED`

### 5.2 quality_flags（run 级，可多值）
```text
PHONE_DATA_MISSING, OAI_DATA_MISSING, CLOCK_SYNC_POOR, CLOCK_DRIFT_HIGH,
PHONE_CHARGING, USB_CONNECTED, WIFI_ON, SCREEN_ON,
CELL_CHANGED, UE_OUT_OF_SYNC, OAI_STALE, CONFIG_CHANGED,
PHONE_THERMAL_CHANGE, HIGH_TEMPERATURE_DRIFT, RSRP_OUT_OF_TARGET
```
`quality_status ∈ {PASS, WARNING, FAILED}`；只标记，不删除数据。

---

## 6. 融合输出

### 6.1 `processed/merged_1s.csv`（统一 1 s 窗口）

```text
window_epoch_ms (t_corrected 对齐到秒), run_id, condition_id, experiment_id, environment, phase,
phone_current_ua_mean, phone_current_ua_median, phone_current_ua_std,
phone_voltage_mv_mean, phone_power_w_mean, phone_energy_j,
phone_rsrp_dbm_median, phone_rsrp_dbm_p10, phone_rsrp_dbm_p90,
phone_sinr_db_median, phone_temperature_c_mean,
gnb_rsrp_dbm, gnb_ssb_sinr_db, gnb_pusch_snr_db, gnb_ph_raw_db, gnb_ph_normalized_db, gnb_pcmax_dbm,
tpc_event_count, tpc_positive_count, tpc_zero_count, tpc_negative_count, tpc_positive_ratio,
pusch_snr_mean, pusch_rssi_mean, pusch_rssi_unit,
ul_mcs_mean, ul_mcs_mode, dl_mcs_mean, qm_mode, n_prb_mean,
harq_initial_tx_delta, harq_retransmission_delta, harq_retransmission_ratio,
ul_bler, dl_bler, dtx, ul_goodput_mbps, dl_goodput_mbps,
phone_tx_bytes_delta, phone_rx_bytes_delta,
quality_flags
```

### 6.2 `processed/run_summary.csv`
每 run 一行：`run_id, condition_id, environment, baseline_energy_j, active_energy_j, tail_energy_j, total_energy_j, energy_per_bit_j_per_bit, j_per_mb, active_seconds, total_goodput_mbps, quality_status, quality_flags`

### 6.3 特征集（禁止数据泄漏，供模型 split）

- `features_m1.csv`：`sample_id, device_id, day, session_id, run_id, environment, orientation, stirrer_state, phone_ss_rsrp_dbm, target(energy)`
- `features_m2.csv`：M1 + `phone_ss_rsrq_db, phone_ss_sinr_db, traffic/throughput, temperature, soc, 其他公开 Android 特征`
- `features_m3.csv`：M2 + `tpc, ph, pcmax, pusch_snr/rssi, mcs, qm, n_prb, harq, bler, dtx, cqi, ri, pmi`
- 同一 `sample_id` 同一 `target energy`，便于 teacher(M3)/student(M2) 配对。

---

## 7. 导出 ZIP 结构（`experiment_<id>.zip`）

```text
manifest.json
devices.csv
conditions.csv
runs.csv
sync.csv
raw/phone/…
raw/oai/snapshots/…
raw/oai/events/…
raw/oai/config/…
processed/merged_1s.csv
processed/run_summary.csv
processed/time_aligned/…
features_m1.csv
features_m2.csv
features_m3.csv
metadata/calibration.csv
metadata/chamber.csv
```

### 7.1 `manifest.json` 必含
```text
experiment_id, created_utc, software_version, app_version, platform_version,
oai_base_url, oai_config(不含 token), device metadata, ac_rc metadata,
clock synchronization summary(sync_rtt_ms, sync_offset_ms, sync_uncertainty_ms, drift),
schema_version, quality_flags
```

---

## 8. AC / RC chamber metadata（人工输入或 CSV 导入，非 OAI 自动获得）

### 8.1 AC（`chamber_metadata_json`）
```text
chamber=AC, phone_orientation_deg, horn_polarization, dut_position,
horn_dut_distance_m, incident_power_density_wm2, incident_field_calibration_id,
reference_antenna_id, rf_calibration_timestamp
```

### 8.2 RC
```text
chamber=RC, stirrer_mode(step|continuous), stirrer_state, stirrer_angle_or_index,
dut_orientation, target_rsrp_dbm, measured_rsrp_statistics,
q_factor(nullable), rms_delay_spread_ns(nullable), k_factor(nullable), loading_condition(nullable)
```

---

## 9. 实验模式预设（template）

`AC_SIGNAL_SWEEP, AC_TARGET_SNR, AC_ORIENTATION, RC_MATCHED_RSRP, RC_TARGET_SNR`
PUSCH target sweep 值由 pilot 决定，**不写死 10/15/20/25**；平台对 phRaw/PH normalized/PCMAX/PUSCH SNR/BLER/DTX 检测失步/不可用区并警告，但不自动改用户设定。
