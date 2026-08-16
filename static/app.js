"use strict";

const $ = (id) => document.getElementById(id);
const history = [];
const maxPoints = 240;
let paused = false;
let lastSample = null;
let speedConfigInitialized = false;

function number(value, digits = 0) {
  return Number.isFinite(value) ? Number(value).toFixed(digits) : "—";
}

function signed(value, digits = 0) {
  if (!Number.isFinite(value)) return "—";
  return `${value > 0 ? "+" : ""}${Number(value).toFixed(digits)}`;
}

function rate(value) {
  if (!Number.isFinite(value)) return "—";
  if (value >= 1000) return `${(value / 1000).toFixed(value >= 10000 ? 1 : 2)} Mbps`;
  return `${value.toFixed(value >= 100 ? 0 : 1)} kbps`;
}

function speed(value) {
  if (!Number.isFinite(value)) return "—";
  return `${value.toFixed(value >= 10 ? 1 : 2)} Mbps`;
}

function text(id, value) {
  $(id).textContent = value ?? "—";
}

function state(id, value, tone = "") {
  const element = $(id);
  element.textContent = value ?? "—";
  element.className = `metric-state ${tone}`.trim();
}

function levelFromDbm(value) {
  if (!Number.isFinite(value)) return 0;
  if (value >= -55) return 5;
  if (value >= -67) return 4;
  if (value >= -75) return 3;
  if (value >= -85) return 2;
  return 1;
}

function levelFromCn0(value) {
  if (!Number.isFinite(value)) return 0;
  if (value >= 40) return 5;
  if (value >= 32) return 4;
  if (value >= 25) return 3;
  if (value >= 18) return 2;
  return 1;
}

function renderBars(id, active) {
  const container = $(id);
  if (!container.children.length) {
    for (let index = 1; index <= 5; index += 1) {
      const bar = document.createElement("i");
      bar.style.setProperty("--bar", index);
      container.appendChild(bar);
    }
  }
  [...container.children].forEach((bar, index) => {
    bar.classList.toggle("active", index < active);
  });
}

function showError(message) {
  $("status-dot").className = "status-dot offline";
  text("device-state", "采集连接异常");
  $("notice-panel").classList.add("error");
  text("notice-title", "采集错误");
  text("notice-text", message || "手机未连接、未授权或 ADB 不可用。");
}

function duration(value) {
  if (!Number.isFinite(value)) return "—";
  const total = Math.max(0, Math.floor(value));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds].map((item) => String(item).padStart(2, "0")).join(":");
}

function renderPowerControl(battery, device) {
  const wireless = /:\d+$/.test(device?.serial || "");
  const usbPowered = battery.usb_powered;
  text(
    "battery-live-level",
    Number.isFinite(battery.level_percent)
      ? `${number(battery.level_percent)} %`
      : "—"
  );
  text(
    "battery-live-charge",
    Number.isFinite(battery.remaining_charge_mah)
      ? `${number(battery.remaining_charge_mah, 1)} mAh`
      : "—"
  );
  text(
    "battery-delta-level",
    Number.isFinite(battery.delta_level_percent)
      ? `${signed(battery.delta_level_percent)} %`
      : "—"
  );
  text(
    "battery-delta-charge",
    Number.isFinite(battery.delta_charge_mah)
      ? `${signed(battery.delta_charge_mah, 1)} mAh`
      : "—"
  );
  text(
    "battery-consumed",
    Number.isFinite(battery.consumed_mah)
      ? `${number(battery.consumed_mah, 1)} mAh`
      : "—"
  );
  text(
    "battery-average-discharge",
    Number.isFinite(battery.average_discharge_ma)
      ? `${number(battery.average_discharge_ma, 0)} mA`
      : "等待 30 秒"
  );
  const baselineTime = battery.baseline_timestamp
    ? new Date(battery.baseline_timestamp).toLocaleTimeString("zh-CN", { hour12: false })
    : "—";
  text(
    "battery-baseline-time",
    `基线 ${baselineTime} · 已记录 ${duration(battery.elapsed_since_baseline_s)}`
  );

  const button = $("prepare-wireless-button");
  if (usbPowered === false) {
    state("power-control-state", "电池供电", "good");
    text("power-control-title", "USB 供电已真实断开");
    text("power-control-message", "正在持续记录电量百分比、电荷计数和平均放电电流；拔线时已自动重置基线。");
    button.textContent = wireless ? `无线 ADB ${device.serial}` : "USB 已断开";
    button.disabled = true;
  } else if (wireless) {
    state("power-control-state", "可拔 USB", "warn");
    text("power-control-title", "无线 ADB 已就绪");
    text("power-control-message", "现在请实际拔掉 USB 数据线。检测到 USB 供电消失后，程序会自动重置电量基线。");
    button.textContent = `无线 ADB ${device.serial}`;
    button.disabled = true;
  } else {
    state("power-control-state", usbPowered === true ? "USB 供电中" : "等待状态", "warn");
    text("power-control-title", "先切换控制链路，再断开 USB");
    text("power-control-message", "点击建立无线 ADB；成功后拔掉数据线，采集会继续运行。手机与电脑必须连接同一 Wi-Fi。");
    button.textContent = "建立无线 ADB";
    button.disabled = false;
  }
}

function render(sample) {
  lastSample = sample;
  if (sample.error) {
    showError(sample.error);
    return;
  }

  const { battery, gps, cellular, wifi, device, collector } = sample;
  const wirelessAdb = /:\d+$/.test(device?.serial || "");
  $("status-dot").className = "status-dot online";
  text("device-name", (device?.model || "Android").replaceAll("_", " "));
  text(
    "device-state",
    collector.mock
      ? "模拟数据 · 实时采集中"
      : `${wirelessAdb ? "无线" : "USB"} / ADB · ${collector.interval_s}s 采样`
  );
  $("notice-panel").classList.remove("error");
  text("notice-title", collector.mock ? "模拟模式" : "测量提醒");
  text(
    "notice-text",
    collector.mock
      ? "当前显示模拟信号。使用 start.ps1 或运行 monitor.py 连接真机。"
      : (battery.usb_powered
          ? "USB 当前仍在给手机供电；要测真实电池消耗，请先建立无线 ADB，再实际拔掉数据线。"
          : "USB 供电已断开；当前读数与电量变化来自电池端，CSV 正在持续记录。")
  );

  text("current-value", number(Math.abs(battery.current_ma), 0));
  text("power-value", `${number(battery.power_w, 3)} W`);
  text("current-raw", `RAW ${signed(battery.raw_current_ma, 1)} mA`);
  state("battery-state", battery.status, battery.status_code === 3 ? "good" : "warn");
  text("voltage-value", `${number(battery.voltage_v, 3)} V`);
  text("level-value", `${number(battery.level_percent)} %`);
  text("temperature-value", `${number(battery.temperature_c, 1)} °C`);
  text("supply-value", battery.usb_powered ? "USB" : (battery.ac_powered ? "AC" : "电池"));
  $("power-line-fill").style.width = `${Math.min(100, Math.abs(battery.current_ma || 0) / 15)}%`;
  renderPowerControl(battery, device);

  state("gps-state", gps.state, gps.has_fix ? "good" : (gps.active ? "warn" : ""));
  text("gps-signal", number(gps.cn0_top4_dbhz, 1));
  text("gps-average", Number.isFinite(gps.cn0_avg_dbhz) ? `${number(gps.cn0_avg_dbhz, 1)} dB-Hz` : "—");
  text("gps-max", Number.isFinite(gps.cn0_max_dbhz) ? `${number(gps.cn0_max_dbhz, 1)} dB-Hz` : "—");
  text("gps-visible", number(gps.satellites_visible));
  text("gps-used", number(gps.satellites_used));
  renderBars("gps-bars", levelFromCn0(gps.cn0_top4_dbhz));
  text(
    "gps-note",
    gps.signal_available
      ? `C/N₀/SNR 来源：${gps.source || "Android GNSS 状态"}。`
      : (gps.active
          ? "MIUI 当前未向 ADB 暴露瞬时 C/N₀；采集器仍会持续检测。"
          : "请让定位应用持续请求 GPS，GNSS 才会进入搜星状态。")
  );

  state("cell-state", cellular.state, cellular.connected ? "good" : "warn");
  const cellSignal = Number.isFinite(cellular.rssi_dbm) ? cellular.rssi_dbm : cellular.rsrp_dbm;
  text("cell-signal", number(cellSignal));
  text("cell-signal-label", Number.isFinite(cellular.rssi_dbm) ? "dBm · RSSI" : "dBm · RSRP");
  text("cell-radio", `${cellular.radio || "—"} / ${cellular.network_type || "—"}`);
  text("cell-rsrp", Number.isFinite(cellular.rsrp_dbm) ? `${number(cellular.rsrp_dbm)} dBm` : "—");
  text("cell-rsrq", Number.isFinite(cellular.rsrq_db) ? `${number(cellular.rsrq_db)} dB` : "—");
  text("cell-sinr", Number.isFinite(cellular.sinr_db) ? `${number(cellular.sinr_db, 1)} dB` : "—");
  text("cell-down", rate(cellular.down_kbps));
  text("cell-up", rate(cellular.up_kbps));
  renderBars("cell-bars", cellular.level ?? levelFromDbm(cellSignal));

  state("wifi-state", wifi.state, wifi.connected ? "good" : "");
  text("wifi-signal", number(wifi.rssi_dbm));
  text("wifi-ssid", wifi.ssid || "—");
  text("wifi-frequency", Number.isFinite(wifi.frequency_mhz) ? `${wifi.frequency_mhz} MHz` : "—");
  text("wifi-tx-link", Number.isFinite(wifi.tx_link_mbps) ? `${number(wifi.tx_link_mbps)} Mbps` : "—");
  text("wifi-rx-link", Number.isFinite(wifi.rx_link_mbps) ? `${number(wifi.rx_link_mbps)} Mbps` : "—");
  text("wifi-down", rate(wifi.down_kbps));
  text("wifi-up", rate(wifi.up_kbps));
  renderBars("wifi-bars", levelFromDbm(wifi.rssi_dbm));

  const instant = new Date(sample.timestamp);
  text("sample-time", `SAMPLE ${sample.seq.toString().padStart(6, "0")} · ${instant.toLocaleTimeString("zh-CN", { hour12: false })}`);
  text("footer-session", `${device?.serial || "LOCAL"} · ${sample.timestamp.slice(0, 10)}`);

  history.push({
    t: instant.getTime(),
    current: Math.abs(battery.current_ma),
    gps: gps.cn0_top4_dbhz,
    cell: cellSignal,
    wifi: wifi.rssi_dbm,
  });
  if (history.length > maxPoints) history.shift();
  $("chart-empty").classList.toggle("hidden", history.length > 1);
  drawChart();
}

function drawChart() {
  const canvas = $("trend-chart");
  const rect = canvas.getBoundingClientRect();
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(1, Math.floor(rect.width * ratio));
  const height = Math.max(1, Math.floor(rect.height * ratio));
  if (canvas.width !== width || canvas.height !== height) {
    canvas.width = width;
    canvas.height = height;
  }
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  const w = rect.width;
  const h = rect.height;
  ctx.clearRect(0, 0, w, h);

  ctx.strokeStyle = "rgba(255,255,255,.055)";
  ctx.lineWidth = 1;
  for (let i = 0; i <= 4; i += 1) {
    const y = 8 + (h - 24) * (i / 4);
    ctx.beginPath();
    ctx.moveTo(0, y + 0.5);
    ctx.lineTo(w, y + 0.5);
    ctx.stroke();
  }
  if (history.length < 2) return;

  const series = [
    { key: "current", color: "#ff5b4d", width: 2.1 },
    { key: "gps", color: "#5dd6c7", width: 1.25 },
    { key: "cell", color: "#5d8eff", width: 1.25 },
    { key: "wifi", color: "#ff9b54", width: 1.25 },
  ];
  const paddingY = 10;
  series.forEach(({ key, color, width: lineWidth }) => {
    const values = history.map((point) => point[key]).filter(Number.isFinite);
    if (values.length < 2) return;
    let min = Math.min(...values);
    let max = Math.max(...values);
    const spread = Math.max(max - min, key === "current" ? 100 : 8);
    min -= spread * 0.15;
    max += spread * 0.15;
    ctx.beginPath();
    let started = false;
    history.forEach((point, index) => {
      const value = point[key];
      if (!Number.isFinite(value)) {
        started = false;
        return;
      }
      const x = history.length === 1 ? 0 : index / (history.length - 1) * w;
      const y = paddingY + (1 - (value - min) / (max - min)) * (h - paddingY * 2);
      if (!started) {
        ctx.moveTo(x, y);
        started = true;
      } else {
        ctx.lineTo(x, y);
      }
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = lineWidth;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  });

  if (history.length > 1) {
    const seconds = (history.at(-1).t - history[0].t) / 1000;
    text("window-label", seconds >= 120 ? `${Math.round(seconds / 60)} 分钟` : `${Math.max(1, Math.round(seconds))} 秒`);
  }
}

async function loadHistory() {
  try {
    const response = await fetch("/api/history?limit=240", { cache: "no-store" });
    const samples = await response.json();
    samples.filter((item) => !item.error).forEach((item) => {
      const cellSignal = Number.isFinite(item.cellular?.rssi_dbm) ? item.cellular.rssi_dbm : item.cellular?.rsrp_dbm;
      history.push({
        t: new Date(item.timestamp).getTime(),
        current: Math.abs(item.battery?.current_ma),
        gps: item.gps?.cn0_top4_dbhz,
        cell: cellSignal,
        wifi: item.wifi?.rssi_dbm,
      });
    });
    if (samples.length) render(samples.at(-1));
  } catch {
    // SSE will take over; the initial request may race server startup.
  }
}

function connectEvents() {
  const source = new EventSource("/api/events");
  source.onmessage = (event) => render(JSON.parse(event.data));
  source.onerror = () => {
    $("status-dot").className = "status-dot offline";
    text("device-state", "等待监控服务");
  };
}

const workloadLabels = {
  idle: "未启动",
  starting: "启动中",
  running: "运行中",
  stopping: "停止中",
  error: "启动失败",
};

function renderWorkloads(workloads) {
  if (!workloads) return;
  ["gps", "wifi", "cellular"].forEach((name) => {
    const item = workloads[name];
    if (!item) return;
    const status = $(`workload-${name}-status`);
    status.textContent = workloadLabels[item.status] || item.status;
    status.className = `load-status ${item.status}`;
    text(`workload-${name}-message`, item.message);
    if (name !== "gps") {
      const sim = name === "cellular" && item.sim_slot
        ? ` · SIM ${item.sim_slot} / subId ${item.subscription_id}`
        : "";
      text(`workload-${name}-interface`, `接口 ${item.interface || "—"}${sim} · ${item.cycles || 0} 轮`);
      text(`workload-${name}-down`, speed(item.last_down_mbps));
      text(`workload-${name}-up`, speed(item.last_up_mbps));
    }
    const button = document.querySelector(`[data-workload="${name}"]`);
    const busy = item.status === "starting" || item.status === "stopping";
    const active = item.status === "running" || item.status === "starting";
    button.disabled = busy;
    button.classList.toggle("active", active);
    if (busy) {
      button.textContent = item.status === "starting" ? "正在启动…" : "正在停止…";
    } else if (active) {
      button.textContent = name === "gps" ? "停止搜星" : "停止测速";
    } else {
      button.textContent = name === "gps" ? "启动搜星" : "启动测速";
    }
  });
  if (workloads.config && !speedConfigInitialized) {
    $("speed-download-url").value = workloads.config.download_url || "";
    $("speed-upload-url").value = workloads.config.upload_url || "";
    $("speed-size-mb").value = workloads.config.transfer_size_mb || 2;
    speedConfigInitialized = true;
  }
}

async function refreshWorkloads() {
  try {
    const response = await fetch("/api/workloads", { cache: "no-store" });
    if (response.ok) renderWorkloads(await response.json());
  } catch {
    // The main device status already reports server connection errors.
  }
}

document.querySelectorAll("[data-workload]").forEach((button) => {
  button.addEventListener("click", async () => {
    const name = button.dataset.workload;
    const active = button.classList.contains("active");
    button.disabled = true;
    try {
      const response = await fetch("/api/workloads/control", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name, action: active ? "stop" : "start" }),
      });
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || "控制操作失败");
      renderWorkloads(result.workloads);
    } catch (error) {
      text(`workload-${name}-message`, error.message);
      button.disabled = false;
    }
  });
});

$("save-speed-config").addEventListener("click", async () => {
  const button = $("save-speed-config");
  const message = $("speed-config-message");
  button.disabled = true;
  message.className = "config-message";
  message.textContent = "正在保存并验证设置…";
  try {
    const response = await fetch("/api/workloads/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        download_url: $("speed-download-url").value,
        upload_url: $("speed-upload-url").value,
        transfer_size_mb: Number($("speed-size-mb").value),
      }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "保存失败");
    speedConfigInitialized = false;
    renderWorkloads(result.workloads);
    message.className = "config-message success";
    message.textContent = "设置已保存。下一次启动 WLAN/蜂窝测速时生效。";
  } catch (error) {
    message.className = "config-message error";
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

$("pause-button").addEventListener("click", async () => {
  const action = paused ? "resume" : "pause";
  try {
    const response = await fetch("/api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    if (!response.ok) return;
    paused = !paused;
    text("pause-button", paused ? "继续采集" : "暂停采集");
    $("status-dot").className = paused ? "status-dot" : "status-dot online";
    if (paused) text("device-state", "采集已暂停");
  } catch (error) {
    showError(error.message);
  }
});

async function powerAction(action, button) {
  button.disabled = true;
  const original = button.textContent;
  button.textContent = action === "prepare_wireless" ? "正在建立连接…" : "正在重置…";
  try {
    const response = await fetch("/api/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "电源测量操作失败");
    if (action === "prepare_wireless") {
      state("power-control-state", "可拔 USB", "warn");
      text("power-control-title", "无线 ADB 已就绪");
      text("power-control-message", "现在请实际拔掉 USB 数据线；采集将通过无线 ADB 继续。");
      button.textContent = result.power_control.adb_endpoint
        ? `无线 ADB ${result.power_control.adb_endpoint}`
        : "无线 ADB 已连接";
    } else {
      text("power-control-message", "电量基线已重置，后续变化将从当前读数重新累计。");
      button.disabled = false;
      button.textContent = original;
    }
  } catch (error) {
    state("power-control-state", "操作失败", "warn");
    text("power-control-title", "未能切换供电测量");
    text("power-control-message", error.message);
    button.disabled = false;
    button.textContent = original;
  }
}

$("prepare-wireless-button").addEventListener("click", (event) => {
  powerAction("prepare_wireless", event.currentTarget);
});

$("reset-baseline-button").addEventListener("click", (event) => {
  powerAction("reset_baseline", event.currentTarget);
});

window.addEventListener("resize", drawChart);
renderBars("gps-bars", 0);
renderBars("cell-bars", 0);
renderBars("wifi-bars", 0);
loadHistory();
connectEvents();
refreshWorkloads();
window.setInterval(refreshWorkloads, 1000);
