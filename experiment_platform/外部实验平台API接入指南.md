# 外部实验平台 API 接入指南

## 1. 接入边界

OAI Control Center 是基础设施层。外部实验平台通过 API 读取 OAI 状态、修改通用参数和启停流量，再在自己的系统内完成功耗、信道、定位、算法或论文专属分析。

请不要向 OAI 主平台回写 `P_phone`、电流/电压模型、实验结论、专属 sweep 状态或 PASS/FAIL 判据。请使用外部平台自己的 experiment ID 将 OAI `requestId`、UTC 时间和外部采样关联。

## 2. Base URL 与访问控制

```text
http://<OAI主机LAN-IP>:8787
```

GET 是只读接口。POST 默认只允许 loopback 和 `OAI_LAN_CONTROL_NETWORKS` 白名单内地址。若 OAI 主机启用了 token，外部请求必须携带：

```http
X-OAI-Control-Token: <shared-token>
Content-Type: application/json
```

8787 使用 HTTP，应只暴露在受信任的实验 LAN。需要跨网络访问时，请在外部增加 VPN 或 TLS 反向代理，不要直接映射到公网。

## 3. 推荐接入顺序

1. `GET /api/health` 确认 API 进程可达。
2. `GET /api/status` 检查 gNB、Core、X410、UE 和当前 traffic session。
3. `GET /api/telemetry/ues` 读取 UE 指标，同时校验 `collection.stale` 和 `ageSeconds`。
4. 用带唯一 `requestId` 的 POST 修改通用 OAI 参数。
5. 需要流量时启动共享 NetworkTest session，轮询 `GET /api/nettest/status`。
6. 实验结束后停止 traffic，读取 events 和 configuration timeline，再将数据保存到外部平台。

## 4. 状态与 telemetry

### 4.1 聚合状态

```http
GET /api/status
```

重点字段：

```text
timestamp
gnb.running / gnb.status / gnb.startedAt
core.running / core.total / core.services[]
radio.frequencyMHz / bandwidthMHz / carrierPrb / txGainDb / rxGainDb
radio.transport.supports100MHz
radio.transport.realtimeTuning.runtimeScheduler
controls.ulScheduler / controls.puschTarget
ues[]
communication.nettest
hardware.x410 / hardware.scope
```

### 4.2 UE telemetry

```http
GET /api/telemetry/ues
GET /api/telemetry/events?limit=100
GET /api/telemetry/config
```

`ues[]` 包含 RSRP、SSB SINR、CQI、RI、PMI、UL-RI、TPMI、PH、PCMAX、PUSCH SNR/RSSI、MCS、Qm、PRB、BLER、HARQ、DTX 和 goodput。

缺失值使用 `null`，不是 0。每次使用前必须检查：

```python
if payload["collection"]["stale"]:
    raise RuntimeError("OAI telemetry is stale")
for ue in payload["ues"]:
    if ue["ageSeconds"] > 5:
        continue
```

兼容别名：

```text
/api/research/ues    == /api/telemetry/ues
/api/research/events == /api/telemetry/events
/api/research/config == /api/telemetry/config
```

新实现应优先使用 `/api/telemetry/*`。

## 5. gNB 与通用参数控制

每次写请求都建议传送外部平台的唯一 `requestId`：

```json
{"requestId":"exp-20260818-001-step-03"}
```

### 5.1 gNB service

```http
POST /api/gnb/service

{"action":"start","requestId":"exp-001-start"}
{"action":"stop","requestId":"exp-001-stop"}
{"action":"restart","requestId":"exp-001-restart"}
```

启动和重启是同步长请求，同时可轮询：

```http
GET /api/gnb/progress?id=exp-001-restart
```

只有返回 HTTP 200 且 `ok=true` 时才表示 NG Setup 和启动稳定窗口通过。

### 5.2 载波与增益

```http
POST /api/gnb/frequency
{"frequencyMHz":3459.36,"restart":true,"requestId":"exp-001-frequency"}

POST /api/gnb/bandwidth
{"bandwidthMHz":100,"restart":true,"requestId":"exp-001-bandwidth"}

POST /api/gnb/gains
{"txGainDb":60,"rxGainDb":40,"restart":true,"requestId":"exp-001-gain"}
```

100 MHz 会在写入前检查 10GbE 和 PCIe 容量。不满足时返回 HTTP 400，不应绕过该保护直接改配置文件。

### 5.3 Scheduler

OAI 自动调度：

```http
POST /api/gnb/ul-scheduler
{"mode":"auto","restart":true,"requestId":"exp-001-scheduler"}
```

手动锁定：

```json
{
  "mode":"manual",
  "mcs":12,
  "qm":6,
  "prb":24,
  "restart":true,
  "requestId":"exp-001-scheduler"
}
```

MCS、Qm 和 PRB 必须一起进入手动模式。客户端不需要传 `mcsTable`，后端根据 MCS/Qm 匹配。

### 5.4 PUSCH Target

`auto` 表示恢复 OAI build 默认 20.0 dB，不表示动态寻优：

```http
POST /api/gnb/pusch-target-snr
{"mode":"auto","restart":true,"requestId":"exp-001-target"}

POST /api/gnb/pusch-target-snr
{"mode":"manual","puschTargetSnrX10":175,"restart":true,"requestId":"exp-001-target"}
```

## 6. NetworkTest

### 6.1 统一 session 模型

```http
GET /api/nettest/status
```

返回：

```json
{
  "ok": true,
  "session": {
    "sessionId": "1f5f...",
    "initiator": "external",
    "state": "RUNNING",
    "running": true,
    "direction": "uplink",
    "protocol": "udp",
    "requestedMbps": 20.0,
    "actualMbps": 19.8,
    "averageMbps": 19.7,
    "bytes": 12345678,
    "startedAt": "2026-08-18T01:00:00+00:00",
    "elapsedSeconds": 5.1,
    "ueIp": "10.0.1.2",
    "errorCode": "",
    "error": ""
  }
}
```

`state` 可为 `IDLE`、`STARTING`、`RUNNING`、`STOPPED`、`COMPLETED` 或 `FAILED`。

### 6.2 外部平台发起

```http
POST /api/nettest

{
  "action":"start",
  "direction":"uplink",
  "protocol":"udp",
  "rateMbps":20
}
```

TCP 不需要 `rateMbps`。UDP `rateMbps=0` 表示饱和发送。

停止任意 initiator 创建的共享 session：

```http
POST /api/nettest
{"action":"stop"}
```

### 6.3 UE / 手机发起

手机必须通过 5G PDU session 访问 OAI 主机：

```http
POST /api/nettest/ue/start

{
  "direction":"downlink",
  "protocol":"udp",
  "rateMbps":20
}
```

这表示“UE 请求开始一个 gNB/PC → UE 的下行 session”，不是 uplink。

```http
POST /api/nettest/ue/stop
{}
```

后端会校验请求源 IP 是当前 UE PDU IP；合法实验 LAN 客户端也可调用，但 `initiator` 会记为 `external`。

### 6.4 错误处理

```json
{
  "ok": false,
  "code": "SESSION_ALREADY_RUNNING",
  "error": "已有测速 session 正在运行"
}
```

| code | 含义 | 建议处理 |
| --- | --- | --- |
| `NO_UE_SESSION` | UPF 无当前 UE PDU session | 等待 UE 注册并重试 |
| `UE_AGENT_UNAVAILABLE` | UE 已接入，但 8420 Agent 不可达 | 只禁用 traffic，不要判定 OAI 离线 |
| `SESSION_ALREADY_RUNNING` | 共享 session 已被占用 | 读取 status，等待或显式 stop |
| `INVALID_DIRECTION` | 方向非 UL/DL | 修正请求 |
| `INVALID_PROTOCOL` | 非 TCP/UDP | 修正请求 |
| `INVALID_RATE` | 速率超界或非有限数 | 修正请求 |
| `LISTENER_FAILED` | PC 侧 socket/listener 失败 | 检查 TEST_HOST、端口和路由 |
| `UE_HANDSHAKE_TIMEOUT` | Agent 已响应，数据面未建立 | 检查 UE firewall/Agent |
| `SESSION_TIMEOUT` | 数据面已建立，但连续 8 秒无数据 | 检查 UE Agent 和 PDU 路由 |
| `UNAUTHORIZED_CLIENT` | 源 IP/token 未通过 | 检查白名单和 token |

HTTP 409 用于 session 冲突，403 用于未授权，503 用于无 UE session，502 用于 Agent 不可达。

## 7. Configuration Timeline

```http
GET /api/history/configuration?limit=100
```

用 `requestId` 将多个参数变更和最终 gNB restart 关联到同一外部实验步骤。当前 timeline 为 API 进程内有界缓存，不是长期实验数据库；外部平台应定期拉取并持久化。

## 8. Python 最小客户端

```python
import json
import urllib.error
import urllib.request

BASE = "http://192.168.31.100:8787"
TOKEN = ""

def request(path, payload=None):
    data = None if payload is None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if TOKEN:
        headers["X-OAI-Control-Token"] = TOKEN
    req = urllib.request.Request(
        BASE + path,
        data=data,
        headers=headers,
        method="GET" if data is None else "POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=70) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        detail = json.load(error)
        raise RuntimeError(f"{detail.get('code', error.code)}: {detail.get('error')}") from error

status = request("/api/status")
if not status["gnb"]["running"]:
    request("/api/gnb/service", {"action": "start", "requestId": "external-start-001"})

telemetry = request("/api/telemetry/ues")
if telemetry["collection"]["stale"] or not telemetry["ues"]:
    raise RuntimeError("No live UE telemetry")

request("/api/nettest", {
    "action": "start", "direction": "uplink", "protocol": "udp", "rateMbps": 20,
})
session = request("/api/nettest/status")["session"]
print(session["sessionId"], session["actualMbps"])
request("/api/nettest", {"action": "stop"})
```

## 9. 兼容性与升级

- 本次没有删除旧 endpoint。
- `/api/status.communication.nettest` 保留旧 `running`、`rateMbps`、`averageMbps`、`bytes`、`elapsedSeconds` 字段，同时新增 session 字段。
- 旧客户端可以不修改；新客户端应迁移到 `/api/telemetry/*` 和 `/api/nettest/status`。
- 前端以 2 s 轮询 `/api/status`，因此 UE 或外部平台启动 traffic 后，Web UI 无需刷新即可显示新 session。

## 10. 验收清单

- [ ] API health 可达。
- [ ] gNB/Core/X410 状态与现场一致。
- [ ] telemetry 不 stale，且 UE `ageSeconds <= 5`。
- [ ] 外部参数 POST 携带唯一 `requestId`。
- [ ] Platform/External 发起 UL 和 DL 流量均可在 status 中观测。
- [ ] UE 发起 UL 和 DL 后 Web UI 自动更新。
- [ ] 两个客户端同时启动时第二个获得 `SESSION_ALREADY_RUNNING`。
- [ ] Web UI、UE 和 External 均能停止当前共享 session。
- [ ] 非 UE/非白名单客户端获得 `UNAUTHORIZED_CLIENT`。
- [ ] gNB restart 前 traffic 自动停止。
- [ ] 外部平台持久化 timeline、telemetry 和自身实验数据。
