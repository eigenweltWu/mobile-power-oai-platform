# 5G Energy Experiment Platform（PC 端实验控制平台）

科研用 5G 智能手机能耗实验系统的 PC 端：实验/任务管理、OAI gNB 控制、手机对时与数据采集、
时间对齐与融合、可视化与导出。与安卓 App（`../android/`）和既有 OAI 控制中心（LAN `:8787`）协同。

## 目录

```text
experiment_platform/
├── backend/                  Python 后端（FastAPI + SQLite）
│   ├── api.py                REST 路由（50+ 端点）
│   ├── server.py             入口（uvicorn）
│   ├── config.py             配置（OAI 地址/token/路径）
│   ├── oai_client.py         OAI :8787 客户端（GET/POST/progress/ensure_gnb_running）
│   ├── oai_models.py         OAI 返回的 pydantic 类型模型
│   ├── db.py                 SQLite 元数据 + 文件 SHA-256 provenance
│   ├── state_machine.py      运行状态机（含 STOPPED 终态）
│   ├── manager.py            实验编排（prepare/sync/arm/collect/import/align/quality/export）
│   ├── task_flow.py          任务流（模板/下发/开始/停止/下行ACK对时/采集/剪辑）
│   ├── phone_channel.py      USB(adb forward)/5G 双通道手机客户端
│   ├── phone_detect.py       手机连接检测（OFFLINE/ATTACHED/CONNECTED）
│   ├── collectors.py         snapshot / event / channel(CIR) / config 采集器
│   ├── sync.py               手机↔PC NTP 式对时 + drift 修正
│   ├── fusion.py             时钟修正 + 1 s 融合 + 能量积分
│   ├── quality.py            运行质量标记
│   ├── export.py             ZIP 导出（manifest + CSV/Parquet + features_m1/m2/m3）
│   ├── templates.py          实验模式预设
│   ├── seed_mock.py          合成（MOCK）dry-run 脚本
│   └── tests/                pytest（22 个用例，全 MOCK 数据）
├── web/                      React 19 + TypeScript 前端（构建到 dist/）
│   └── src/pages/            Dashboard / Experiments / RunPlanner / RunDetail /
│                             Comparison / Matrix / Data / Settings / Export / Timeline
├── data/                     运行期数据（raw/ processed/ platform.db）
└── config.local.json         本地配置（OAI host/port/token）
```

## 环境要求

- Python 3.14+；Node 20+ / npm
- 本机 Android platform-tools（`adb`，用于 USB 通道）

## 快速开始

```powershell
# 1. 安装 Python 依赖
python -m pip install fastapi "uvicorn[standard]" pydantic httpx pandas pyarrow pytest

# 2. （可选）本地配置 OAI 地址/token
Copy-Item config.local.json.example config.local.json
#   编辑 config.local.json 填入 OAI 主机 IP

# 3. 构建前端（首次 / 改过前端后）
cd web; npm install; npm run build; cd ..

# 4. 启动平台（API + 前端页面）
python -m experiment_platform.backend.server
# 浏览器打开 http://127.0.0.1:8900
```

## 用户配置

### OAI 连接（3 种方式，优先级从高到低）

1. **环境变量**：`OAI_HOST`、`OAI_PORT`、`OAI_CONTROL_TOKEN`、`OAI_TIMEOUT_S`。
2. **`config.local.json`**（不入库、不提交 git）：
   ```json
   {
     "oai_host": "192.168.3.7",
     "oai_port": 8787,
     "oai_control_token": "",
     "oai_timeout_s": 8.0,
     "listen_host": "127.0.0.1",
     "listen_port": 8900
   }
   ```
3. **网页 Settings 页**：`GET/PUT /api/settings` 可在线改 OAI host/port/token。

> OAI 主机 IP 会随路由器/网段变化（例如 `192.168.31.119` ↔ `192.168.3.7`）。
> 改网段后，只需改这里 + 重跑路由脚本（见下），平台即可重新连上 OAI。
> token 只会存本地，**绝不写进数据库、不导出、不提交 git**。

### 其它环境变量

`PLATFORM_HOST`、`PLATFORM_PORT`、`PLATFORM_DATA_DIR`、`PLATFORM_WEB_DIST`。

---

## 路由表配置（PC ↔ 手机 5G 直连，不依赖 USB）

手机注册在 OAI 5G 网络后拿到 PDU IP（如 `10.0.1.24`）。要让 PC 不连 USB 也能直连手机，
需要两级路由：

### ① OAI 主机（Linux，一次性/网段不变时持久）

OAI 5GC 的 docker 网段 `192.168.70.128/26`，UPF 固定 IP `192.168.70.134`（docker-compose 写死）。
手机 UE 网段 `10.0.0.0/24` / `10.0.1.0/24` / `10.0.9.0/24` 锚定在 UPF 的 `tun0`。

```bash
# 路由：UE 网段 → UPF
sudo ip route add 10.0.0.0/24 via 192.168.70.134
sudo ip route add 10.0.1.0/24 via 192.168.70.134
sudo ip route add 10.0.9.0/24 via 192.168.70.134

# 转发：放行 LAN → docker 网桥（默认 FORWARD DROP，需显式放行）
sudo iptables -I FORWARD -i wlo1 -o oai-cn5g -j ACCEPT   # wlo1 换成 OAI 主机的 LAN 网卡

# 持久化（重启后自动恢复）
sudo sh -c 'iptables-save > /etc/iptables/rules.v4'
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent
```

> `ip_forward` 需为 1：`sysctl net.ipv4.ip_forward=1`。
> 这些是纯路由/iptables 配置，**不修改 OAI 核心网代码**。

### ② Windows PC（每次网段变化后重跑）

提供脚本 `.tools/setup_routes.ps1`（UTF-8 BOM，管理员运行）：

```powershell
powershell -ExecutionPolicy Bypass -File E:\Pythonprojects\MOBILE\.tools\setup_routes.ps1
# 提示输入 IP 时，输入 OAI 主机当前地址（如 192.168.3.7）
```

脚本会：清理旧的 `10.0.x` 路由 → 提示输入 OAI 主机 IP → 添加三条持久路由
`10.0.0.0/24`、`10.0.1.0/24`、`10.0.9.0/24` 指向该 IP。

验证：`ping 10.0.1.24`（手机实际 PDU IP，从 UPF 会话表/`adb shell ip route` 查得）。

---

## 手机连接检测（三态）

任何涉及手机的操作（下发/开始/停止/采集）都会先经 `phone_detect.detect_phone()` 检测：

| 状态 | 含义 | 检测方式 |
|------|------|----------|
| CONNECTED | 5G 可达（实验中且有应答） | 通过 PDU IP `:8420` 访问手机 agent |
| ATTACHED | USB 连接 | `adb devices` 检测到手机 |
| OFFLINE | 两者都不通 | — |

- CONNECTED 与 ATTACHED **可同时成立**（USB 连着 + 5G 也通）。
- 手机操作自动选通道：CONNECTED 走 5G（PDU IP），否则 ATTACHED 走 adb forward；都不可达则报错 `phone OFFLINE`。

---

## 核心实验流程

```text
创建任务(目的/流程/初始OAI配置) → 配置 OAI 模板(+删改)
  → 下发任务到手机 → PC 开始实验(自动启动 gNB + 等 UE in-sync + 持续发下行)
  → 手机开始实验(环境监测) → 收到下行后回上行 ACK → 双方记录时间戳(对时)
  → 手机大流量上下行 → 中途可改 PUSCH Target/Qm → 双方各自停止(记录时间戳)
  → 重连 USB → PC 按 UUID 匹配采集 → 对齐/融合/质检 → 时间轴可视化/剪辑/导出
```

关键端点：

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/api/experiments` | 建实验（含 purpose/flow/initial_oai_config） |
| GET/POST/DELETE | `/api/experiments/{id}/templates` | OAI 模板 CRUD |
| PUT | `/api/experiments/{id}` | 编辑任务 |
| POST | `/api/experiments/{id}/push` | 下发任务到手机 |
| POST | `/api/experiments/{id}/start` | 开始实验（自动启 gNB + 下行） |
| POST | `/api/runs/{id}/stop` | **完整停止**（停下行+采集器+手机+gNB，置 STOPPED） |
| GET | `/api/phone/tasks?serial=` | 手机实验清单 + 本机是否已采集 |
| POST | `/api/experiments/{id}/collect` | 从手机采集（记录主机名/时间/次数） |
| GET | `/api/experiments/{id}/timeline` | 时间轴数据（样本 + ACK 时间戳 + 剪辑） |
| POST | `/api/experiments/{id}/clip` | 剪辑并保存 |
| DELETE | `/api/runs/{id}` | 删除结果 |
| GET | `/api/platform/status` | Dashboard 状态（含手机三态） |

## 数据分级与导出

- Level 0 raw：`data/raw/phone/`、`data/raw/oai/`（原始 JSON/CSV，永不覆盖）。
- Level 1：`data/processed/time_aligned/`（时钟修正后）。
- Level 2：`data/processed/merged_1s/`（统一 1 s 分析表）。
- 导出：`GET /api/experiments/{id}/export` → ZIP（manifest + raw + processed + features_m1/m2/m3 + parquet）。

## PHY 快照（IQ / CIR / Delay-Doppler）

平台只读取 OAI 控制后端缓存的 `GET :8787/api/scope/snapshot`，且最多 1 Hz。
外部平台不得连接 `:8090`、`/softscope`、WebScope REST，亦不得使用旧的
`:8091/channel` 代理。使用快照前必须确认 `connection == "live"` 且
`ageSeconds <= 3`；重复读取缓存不会新建 WebSocket 或触发 PHY 采集。

## 测试

```powershell
python -m pytest experiment_platform/backend/tests -q        # 22 passed
python -m experiment_platform.backend.seed_mock               # 合成(MOCK) dry-run
python verify_system.py                                       # 整体验收检查
```

测试全部用**明确标记 MOCK/TEST** 的合成数据，绝不冒充真实实验数据。

## 设计文档

- `../SYSTEM_DESIGN.md`、`../DATA_SCHEMA.md`、`../EXPERIMENT_WORKFLOW.md`、`../RUNBOOK.md`
