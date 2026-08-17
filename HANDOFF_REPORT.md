# 任务迁移报告 — 5G 能耗实验平台（OAI / 手机 / 混响室）

> 生成时间：2026-08-17　|　当前仓库：`C:\Simulation\MOBILE`（master 分支）
> 本报告自包含迁移所需全部上下文：环境、已完成工作（含 commit）、当前测试状态、待办事项与关键工程约束。

---

## 0. 2026-08-17 本轮续作结果

- 已安装/准备 `deploy.ps1` 所需依赖：Python 包、前端 npm 依赖、Microsoft JDK 17、Android command-line tools/platform/build-tools、Gradle wrapper；免安装工具位于被忽略的 `.tools/`。
- `deploy.ps1` 已支持自动发现仓库内 JDK/Android SDK、Gradle wrapper、自检后台后端启动，以及 APK 安装失败显式退出。本轮因手机已有 APK 签名不同，未卸载、未覆盖手机数据。
- 平台后端已在 `127.0.0.1:8900` 运行并加载 RC/Stirrer 新端点；模拟搅拌器 connect → +5° move → disconnect API 冒烟通过。
- `a405868 fix(gNB): stabilize scope and RRC reestablishment` 经 Git 对比与日志回放确认引入了 RLC/DRB 恢复回归，已由 `fb7f126` 完整回滚；不要恢复该提交。未验证的第一次 CIR 草案保留在远端 `stash@{0}`，仅供审计，不应应用。
- OAI 新修复 `6c9e006 fix(scope): snapshot UL CIR outside the PUSCH hot path`：为 Web CIR 预分配双缓冲，Web 刷新周期原子请求一次，下一次 antenna-0 PUSCH 只做一次有界 `memcpy` 后原子发布；不再在每个 PUSCH 上分配、加锁和轮换通用 scope buffer，原生 XForms scope 行为保持不变。
- CIR daemon 保持约 5 秒刷新和按需连接：`GET /channel` 激活采集，无请求 15 秒后断开；`GET /status` 只读。服务空闲不占用 webscope，激活后连续返回新鲜的 4096 点复数 CIR。
- 当前 OAI：gNB running、核心网 10/10、UE in-sync；AMF `5GMM-REGISTERED`、SMF `PDU_SESSION_ACTIVE`、UE IPv4 `10.0.1.13`；scheduler=auto、PUSCH target=auto/20 dB。最近 5 分钟 RLF、RRC 重建、LCID4 ignore、SRB/DRB max RETX、assert/exit 均为 0。
- 并存压力实测：4 路 ADB 临时 UDP 发送把当前无线链路压到约 **12.8–13.1 Mbps**（该链路的实际饱和点），同时 CIR 每 5 秒更新、手机公网 ping 25/25（0% 丢包）；结束时 UL BLER≈5.8%，上述故障计数均为 0。饱和时平均 RTT≈574 ms 是队列拥塞，不是 CIR 中断。
- 平台防护：普通模板默认由 999 Mbps 饱和改为 **5 Mbps CBR**；只有显式配置 `ulTrafficMbps >= 100` 才请求饱和。手机计划序列化补齐 `idleSeconds`、`collectionSeconds`、`ulTrafficMbps`，UL CBR 改为 10 ms 有界突发节拍，解除旧实现约 8 Mbps 的单包延时上限。
- 最终测试：后端 `50 passed`；前端生产构建与 Android `assembleDebug` 成功；前后端已由 `deploy.ps1 -SkipAndroid` 重部署（后端 PID 26800）。手机公网 ping 4/4、0% 丢包，App HTTP 状态可读且保持 IDLE。
- 手机操作约束：只允许通过 ADB 启动 App、读状态、临时发测试流量，以及开关飞行模式；不得改 APN/DNS/路由或其他手机设置。本轮没有修改手机网络设置。
- Android 新 APK 尚未安装：现有 App 与本地 debug keystore 签名不一致，`adb install -r` 返回 `INSTALL_FAILED_UPDATE_INCOMPATIBLE`。为保护现有数据没有卸载 App。要部署新 pacing/序列化代码，需找回原签名 keystore，或由操作者明确批准先导出数据再卸载重装。

---

## 1. 系统组成与环境

| 组件 | 位置 | 说明 |
|---|---|---|
| **实验平台（PC）** | `C:\Simulation\MOBILE\experiment_platform` | Python FastAPI 后端（`127.0.0.1:8900`）+ React/Vite 前端（`web/dist`，构建后由后端托管） |
| **手机 App** | `C:\Simulation\MOBILE\android` | Kotlin，AgentServer（HTTP，端口 8420），Room 数据库，serial `53616213` |
| **OAI 控制中心** | Linux 主机 `192.168.31.119`，`/home/usrp2/桌面/OAI(1)/oai-control-center` | 独立 git 仓库；控制 API `:8787`，信道 CIR daemon `:8091` |
| **混响室搅拌器** | 参考代码 `measurement system/AntennaTurntableController` | `MT_API.dll`（**32 位**，USB，位置模式，**3200 步/度**）；备选 `StirrerDll.dll`（串口，50000 步/360°，未采用） |

端口约定（PC 端 adb forward，互相隔离避免隧道互踩）：
- `8420` = 实验期 5G 直连 DownlinkLoop（live）
- `9420`（pc_port+1000）= 手机探测
- `10420`（pc_port+2000）= USB 数据提取（inventory / per-run pull）

---

## 2. 本阶段已完成的任务与对应 commit

### 2.1 版本管理 ✅
两端工作文件夹已整理清理并提交；约定**每次修改+测试完成后 commit**。

### 2.2 UDP UL/DL Traffic Saturation ✅ `5256f91` + 本轮修复
- **OAI 端**（Linux 主机，独立仓库）：`NetworkTest` 类新增 UDP 协议 + Saturation 模式（尽可能快发包饱和链路），`start` 接收 `protocol`/`rate_mbps`；前端 `app/page.tsx` 增加协议选择与速率输入。
- **平台端**：`templates.py` 所有模板 fixed 列表加入 `ulTrafficMbps`；本轮将默认值改为安全的 **5 Mbps CBR**，≥100 才显式触发 UL saturation；`Experiments.tsx` 已同步标签与默认值。
- 手机端 workload 引擎读取 plan 中的 `ulTrafficMbps`（SharedPreferences）。本轮修复 AgentServer 的 plan JSON 字段遗漏，并把 CBR pacing 改为 10 ms 有界 burst；代码已构建，但受签名不匹配阻挡，尚未覆盖安装到手机。

### 2.3 手机端：无 run_id 记录舍弃 ✅ `69cd7cf`
- `AppDatabase.kt`：`SampleDao.deleteUnarmed` / `MarkerDao.deleteUnarmed`。
- `ExperimentService.stopExperiment`：未收到平台 runId 时 `flushBuffer(discardEid)` 舍弃。
- 实测验证：9 秒监控会话的 41 样本 + 1 标记被成功删除。

### 2.4 平台端：USB 数据同步 ✅ `3ce67f1`
- 手机端新增 `GET /agent/data/inventory`（TaskStore + Room run 摘要）；`export()` 支持 `?runId=` 定向导出历史 run。
- 平台端 `task_flow.py`：`_phone_usb()`（专用 10420 隧道）、`phone_inventory()`（手机↔平台交集标记）、`pull_phone_run()`（按 run 提取，重复拉取幂等）。
- 前端 `PhoneSync.tsx`：手机实验卡片、展开 run 列表、交集标记、提取按钮。

### 2.5 时间戳融合 + 视频剪辑式 UI ✅ `0fcf399`
- `manager.py`：`clip_t0()`（t=0 = 首次对时 sync_anchor，回退最早手机样本/run start）；`clip()` 融合 phone_samples + oai_snapshots + oai_channel → 单 CSV（`t_s` 秒轴），clips 表另存副本。
- `Timeline.tsx`：`ClipWorkbench` — 拖动色块手柄调起止、多片段、关键时间戳标注、另存副本。

### 2.6 混响室（RC）采样采集 ✅ 代码完成 `117c0e1`（**E2E 测试未做，见 §4**）

与暗室（AC：连续记录+后期剪辑）不同，RC 为**逐样本采集**。新文件：

| 文件 | 内容 |
|---|---|
| `backend/stirrer.py` | `StirrerAgent`：ctypes 无法直接加载 32 位 DLL → 内嵌 C# 源码，用系统自带 `csc.exe`（`C:\Windows\Microsoft.NET\Framework64\v4.0.30319`，`/platform:x86`）编译 `data/tools/StirrerAgent.exe`（stdin/stdout JSON 行协议），旁置 `MT_API.dll`。**simulate 模式**（虚拟电机）用于无硬件全流程演练。3200 步/度。 |
| `backend/rc_flow.py` | `RcCampaign`（后台线程）完整流程：①**底噪标定**（N 帧 CIR 逐 tap 中值，剔除最强 10% 信号 tap → floor）→ 循环每样本：②Stirrer `move_rel_and_wait`（等静止）+ 机械稳定 → ③**puschTargetSnrX10 微调伺服**（`gnb_pusch_target_snr(restart=False)`，CIR 峰值 tap 功率作 RSSP，误差>容差按 ±step 调，最多 N 次）→ ④**手机定时记录窗**（`/agent/session` 下发 per-sample plan[settle, dwell, 0] + `/agent/rearm`，轮询 phase 精确卡 LOADED 边界；同 runId 汇入同一 run）→ ⑤窗口结束立即抓 CIR，**按 floor+margin 过滤 taps**，算 RMS/平均延迟、tap 数、峰值 + 窗口内 gNB 汇总（goodput/BLER）→ 写 `rc_samples` 表 + 原始 JSON（`raw/rc/<run>/sample_XXX.json`，含 servo_log）。 |
| `backend/db.py` | 新表 `rc_samples`（stirrer 角度、pusch_x10、RSSP/误差、底噪、原始/过滤 tap 数与延迟、窗口时间戳、servo_log、gnb_summary）。 |
| `backend/api.py` | `GET /api/stirrer/status`、`POST /api/stirrer/{connect,disconnect,move,stop}`、`POST /api/rc/campaign/{start,stop}`、`GET /api/rc/campaign/status`、`GET /api/rc/samples`。 |
| `web/src/pages/Chamber.tsx` + `App.tsx` | 新导航页 **Chamber RC**：搅拌器连接/手动步进（真实/模拟切换）、采集配置面板（步进角/样本数/dwell/目标 RSSP/容差/伺服参数/底噪帧数与余量）、实时日志轮询、样本历史表（含 gNB UL/BLER 列）。 |

**AC/RC 区分**：实验 `environment` 字段（前端已有 AC/RC 徽章）；RC 数据在 `rc_samples` 表（采样式），AC 数据在 phone_samples/clips（连续式）。

### 2.7 更早期的相关修复（本仓库历史 commit，迁移后仍需知晓）
- `0704fff`：gNB 重启 fire-and-poll（30s+ 重启不再超时，轮询 `/api/gnb/progress`）；UE IP 用**最新 session id**（非最大 IP）；ChannelCollector 1s 挂载。
- OAI 端（Linux 仓库）：`websrv_scope.c` scopeData 死锁修复（`gNB==NULL` 才 defer；`scopeData==NULL` 时调 `gNBinitScope()`，已重编 libwebsrv.so）；`libnrscope.so` 改名规避无头环境 X11 SIGSEGV(139)。

---

## 3. 当前测试状态（截至报告生成）

| 项 | 状态 |
|---|---|
| Python 模块导入（stirrer/rc_flow/api） | ✅ |
| StirrerAgent.exe 编译（csc x86） | ✅（含 srcsha 哈希缓存，源变更自动重编） |
| 模拟模式 open/move/position/status | ✅ |
| 真实 USB 模式 | ⚠️ 本机 `MT_Check=-1`（控制器在混响室，未接本机）；`open()` 已改为 check 失败即干净报错 |
| 前端 `npm run build`（tsc+vite） | ✅ |
| **后端进程** | ✅ 已重部署新代码，`127.0.0.1:8900`，PID 26800 |
| 手机 | ✅ USB/ADB online，App server 可读；当前 `IDLE`、`monitoring=false`，公网正常 |
| gNB | running ✅（192.168.31.119） |
| CIR + 通信饱和并存 | ✅ 12.8–13.1 Mbps 实际饱和、4096 点 CIR 连续刷新、公网 0% 丢包、无 RLF/LCID4/max RETX |
| RC campaign E2E | ⏳ **未执行**（需要 App 进入 monitoring；部署新 APK 还需原签名） |

---

## 4. 待办事项（迁移后从这里继续）

1. **解决 Android 签名**：优先找回原签名 keystore；否则先完整导出 App 数据，并由操作者明确批准卸载/重装。不要直接卸载现有 App。
2. **手机进入监控模式**（App 中点“开始任务”）。
3. **RC 全流程验证并存档**（建议首跑参数）：
   - 建一个 `environment=RC` 的实验 + 模板（先用 `ulTrafficMbps:5`、schedulerMode=auto；需要压力实验时再显式改为 ≥100）；
   - 平台启动实验（gNB 强制重启→UE 回附着→shake 对时→sync-confirm→phone ARMED）；
   - Chamber 页启动采集：`simulate_stirrer=true, n_steps=2, step_deg=5, dwell_s=15~20, settle_s=5, noise_frames=10`；
   - 验证：`rc_samples` 两行（角度递增、RSSP 误差列、过滤 tap 数）、`raw/rc/<run>/sample_00X.json`、手机同 runId 样本含 LOADED 段、campaign status 到 `completed`；
   - USB 拉取该 run 数据 → Timeline 复核。
4. **测试通过后 commit**（约定：每次修改+测试完成即 commit）。
5. （后续）混响室实机：搅拌器控制器 USB 接入 PC，`simulate=false` 全流程复测。

---

## 5. 关键工程约束（迁移必读，违反会复现已踩过的坑）

1. **MT_API.dll 是 32 位**：必须经 x86 helper EXE 调用；64 位 Python ctypes 直接加载必败。3200 步/度。
2. **pusch 伺服必须 `restart=false`**：每次样本微调若触发 gNB 重启（30s+、UE 重附着）会摧毁采样节奏。
3. **`/agent/rearm` 仅 IDLE 相位可用**（手机端强校验）；手机必须已 ARMED/RUNNING 且操作者已点"开始任务"（monitoring）。per-sample 窗口 = `session(新plan)` + `rearm`，runId 不变。
4. **实验期所有 live 通道（sync/phase/rearm/throughput）只走 5G PDU，禁 USB 回退**；USB 仅实验后数据同步，且用独立端口 10420。
5. **每次实验启动强制 gNB 真重启**（apply_condition force_restart=True，校验 startedAt 变化）；重启后 UE 换 PDU IP，靠 `/api/shake` 重解析。
6. **TaskFlow._phone() yield 元组** `(PhoneAgent, detection)`——`as (agent, ph)` 解包，否则 AttributeError 被吞成 unknown。
7. 后端改代码后**必须重启进程**才生效；本轮 `deploy.ps1 -SkipAndroid` 已能完成重启与自检。
8. oai_channel_daemon(8091) 会缓存最后一帧 CIR；gNB scope 断开后 `ok:false`，graph 重新注册后自动恢复——排查先看 `graphs:[]`。
9. 不得自动修改手机网络配置；只允许 ADB 启动/读取/临时测试流量和开关飞行模式。实验中拔 USB（开始监控前手机会提示）。
10. Kotlin 编译用 `./gradlew.bat compileDebugKotlin -Dkotlin.compiler.execution.strategy=in-process`（沙箱限制 daemon 临时文件）。

---

## 6. 快速上手命令（新平台）

```powershell
# 后端（改代码后必须重启）
cd C:\Simulation\MOBILE; python -m experiment_platform.backend.server

# 前端构建（产物由后端托管）
cd experiment_platform\web; npm run build

# 搅拌器 helper 手动重编（正常自动）
cd experiment_platform\data\tools; & C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /platform:x86 /optimize+ /out:StirrerAgent.exe StirrerAgent.cs

# 冒烟：模拟搅拌器
python -c "from experiment_platform.backend.stirrer import StirrerAgent; from experiment_platform.backend.config import load_settings; a=StirrerAgent(load_settings(), simulate=True); print(a.open(), a.move_rel_and_wait(5), a.status())"
```

核心 API 速查：
- `POST /api/experiments/{id}/start`（body: serial/pc_port/collection_seconds/idle_seconds/template_id）
- `POST /api/rc/campaign/start`（body: experimentId + §2.6 配置面板全部字段）
- `GET /api/rc/campaign/status?experiment_id=` / `GET /api/rc/samples?experiment_id=`
- `POST /api/phone/pull`（body: experimentId/runId/serial）· `GET /api/phone/tasks?serial=`
- `POST /api/experiments/{id}/clip`（start_ms/end_ms 相对 t0）
