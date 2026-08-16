# 红米 K30i 暗室无线功耗监控

> **新增（2026-08-15）**：本仓库现已包含完整的 **5G Smartphone Energy Experiment System**。
> 设计文档见 `SYSTEM_DESIGN.md` / `DATA_SCHEMA.md` / `EXPERIMENT_WORKFLOW.md`；
> PC 实验平台在 `experiment_platform/`（Python 后端 + React 前端，见 `experiment_platform/README.md`）；
> Android 采集 App 在 `android/`。下面内容为旧版 ADB 监控工具（`monitor.py`）说明。

这是一个完全在本机运行的监控工具。Python 通过 ADB 读取手机数据并记录 CSV，HTML/JavaScript 页面使用 SSE 实时更新，不需要安装 Python 第三方包。

## 快速启动

1. 保持手机“开发者选项 / USB 调试”开启，并在手机上允许这台电脑调试。
2. 在 PowerShell 中进入本目录，执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\start.ps1
   ```

3. 浏览器会打开 `http://127.0.0.1:8765`。数据保存在 `data/session-日期时间.csv`。

当前目录已准备好 Android Platform Tools。若以后移动项目且 `.tools` 不存在，`start.ps1` 会从 Google 官方地址重新下载。

## 断开 USB 充电并继续采集

这台未 Root 的 Redmi K30i 不允许 ADB shell 写入充电控制节点。`dumpsys battery unplug` 只会伪造 Android 上报的供电状态，并不会真实停止充电，因此本工具不会使用该命令。

无 Root 的可靠流程：

1. 保持电脑和手机连接同一个 Wi-Fi。
2. 在仪表盘“USB 供电隔离与电量变化”中点击“建立无线 ADB”。
3. 页面显示“可拔 USB”后，实际拔掉 USB 数据线。
4. 程序检测到 USB 供电消失时会自动重置电量基线，并继续通过无线 ADB 采样。

页面和 CSV 会逐个采样记录当前电量百分比、当前剩余电荷 mAh、原始 charge counter、电量/电荷变化、累计消耗 mAh 和按基线计算的平均放电电流。也可以随时点击“重置电量基线”开始新的测量段。

如果必须保留 USB 数据连接同时断开供电，需要使用能独立切断 VBUS 的 USB 数据/供电隔离硬件，或 Root 后使用该内核实际提供的充电控制节点。无线 ADB 依赖 Wi-Fi，因此在拔掉 USB 后不能同时使用本工具当前的蜂窝主动测速（该测速需要临时关闭 Wi-Fi）；GNSS、被动蜂窝信号记录和 Wi-Fi 测试不受影响。

也可以直接启动：

```powershell
python .\monitor.py --open
```

常用参数：

```text
--serial SERIAL       多台手机时指定设备；也可填无线 ADB 的 ip:port
--interval 1.5        采样间隔，最小 0.5 秒
--port 8765           本地网页端口
--no-log              不写 CSV
--mock                使用模拟数据预览界面
--speed-size-mb 2     每轮 WLAN/蜂窝测试各下载和上传的数据量
```

## 主动负载控制

仪表盘中的“手机主动负载控制”可以从电脑端启动或停止：

- 持续 GNSS 搜星：Redmi K30i 上优先自动打开手机内置的 CIT GPS 测试并定位到“GPS 测试”项，不安装应用。其他机型没有兼容 CIT 页面时，才会安装开源 [GPSTest](https://github.com/barbeau/gpstest) 辅助应用。停止时会结束测试页；如果程序代为打开了系统定位开关，也会恢复原状态。
- WLAN 双向测速：只在 Wi-Fi 已连接时启动，`curl` 强制绑定 `wlan*` 接口，循环进行定量下载和上传。
- 4G/5G 双向测速：从 Android Connectivity 中选择同时具备 `CELLULAR + INTERNET + VALIDATED` 的通用数据 APN，排除 IMS 接口，并映射到具体 SIM 卡槽。测速期间程序会暂时关闭 Wi-Fi，使 ADB shell 的 Android 策略路由切换到数据卡；停止、失败或退出时自动恢复 Wi-Fi。

默认测速端点为 Cloudflare Speed Test，每轮默认下载 2 MB、上传 2 MB。它会产生真实网络流量；可以在启动时改用实验室内的 HTTP 测速服务器：

```powershell
python .\monitor.py --speed-size-mb 5 `
  --speed-download-url "http://192.168.1.10/down?bytes={bytes}" `
  --speed-upload-url "http://192.168.1.10/up"
```

停止监控服务时，程序会停止所有负载并清理手机上的临时上传文件。GPSTest 应用会保留，避免下次重复安装。

双卡环境中会显示实际使用的 `SIM / subId / rmnet_data*`。例如本机当前检测到 SIM 1（subId 2，中国移动）使用 `rmnet_data1` 作为通用公网 APN，而 `rmnet_data2` 是 IMS 专用接口；SIM 2 是测试白卡。

## 指标含义

- 电流：Android `BatteryProperties.CURRENT_NOW` 的瞬时电池端电流。程序保留 OEM 原始符号，并结合电池状态统一显示“充电/放电”和绝对功率。
- 电压：Android Battery Service 返回的电池电压。
- 传输速率：按 `wlan*`、`rmnet_data*` 等网卡累计字节的相邻采样差计算，是实际吞吐率；“协商发送/接收”是 Wi-Fi 物理链路协商速率，两者不是同一个概念。
- LTE：优先显示 RSSI，同时记录 RSRP、RSRQ、SINR。
- 5G NR：Android 通常不提供传统 RSSI，页面会明确改用 SS-RSRP，并同时记录 SS-RSRQ 和 SS-SINR。
- GPS：强度使用卫星 C/N₀（dB-Hz），不是 RSSI。Top 4 是最强四颗卫星的平均 C/N₀。

## 本机实测注意事项

- 这台 Redmi K30i 5G 的电流节点对 ADB shell 无读取权限，但系统 BatteryProperties Binder 接口可正常返回瞬时电流，本程序已经针对该格式实现并验证。
- 当前测试白卡未注册网络时，`OUT_OF_SERVICE`、空 LTE/NR 信号和 0 kbps 都是正常数据，不会被当成采集错误。
- MIUI 只有在某个定位应用持续请求 GPS 时才会让 GNSS 引擎搜星。启动持续搜星后，程序会从 CIT 前台状态读取卫星 SNR，作为 Android 旧接口中的 C/N₀ 等效值；没有卫星时保留为空，不会伪造为 0。
- 启动持续 GNSS 后，CIT/GPSTest 会保持手机测试页在前台，屏幕功耗会进入测量结果；建议固定亮度并做一组相同屏幕状态的基线对照。
- USB 连接本身会供电和充电。Android 返回的是电池端电流，无法单独反推出“手机总负载 + 电池充电”各自所占的电流。需要绝对整机耗电时，应以外部程控电源/功耗仪为准，或按上面的无线 ADB 流程断开 USB 再测。
- 暗室可能同时屏蔽 GNSS、蜂窝和 Wi-Fi。建议先在室外或已知信号环境完成一次对照采样，确认所有指标能出现，再进入暗室。
