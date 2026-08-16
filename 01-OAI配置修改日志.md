# OAI 核心网、gNB 与射频配置修改日志

> 日期：2026-08-13  
> 范围：OAI 5GC、gNB、USRP X410/X310、天线、频率及掉线排查。

## 1. 当前有效配置

| 项目 | 当前值 |
| --- | --- |
| 网络 | 5G SA，n78 |
| PLMN | MCC `466` / MNC `92` (`46692`) |
| TAC / PCI | `1` / `0` |
| gNB ID / Cell ID | `0xe00` / `12345678` |
| S-NSSAI | SST `1`，SD `0xFFFFFF` |
| 中心频率 | **3459.36 MHz** |
| SSB / Point A ARFCN | `630624` / `629352` |
| 带宽 | 106 PRB，30 kHz SCS，标称 40 MHz |
| 有效占用带宽 | 38.16 MHz |
| RU | 1 TX / 1 RX |
| TX / RX gain | 60 dB / 40 dB |
| X410 | `192.168.100.2`，internal clock/time |
| gNB 容器 | `oai-gnb-runner` |
| 核心网 | 10/10 容器运行 |
| Web SoftScope | `127.0.0.1:8090`，gNB 以 `--websrv` 启动 |

当前 gNB 实际配置文件：

```text
/home/usrp2/桌面/gnb1.sa.band78.fr1.106PRB.prs.usrpx310_oai.conf
```

备份：

```text
/home/usrp2/桌面/gnb1.sa.band78.fr1.106PRB.prs.usrpx310_oai.conf.dashboard.bak
```

项目目录中的 `gnb1.sa.band78.fr1.106PRB.prs.usrpx410_OAI_Android.conf` 是示例/历史文件，当前容器未使用它。

## 2. OAI 5GC

核心网由 `oai-cn5g_Android/docker-compose.yaml` 管理。Docker 网络为 `oai-cn5g-public-net`，子网 `192.168.70.128/26`，Linux bridge 为 `oai-cn5g`。

| 服务 | 容器 | IP / 端口 |
| --- | --- | --- |
| NRF | `oai-nrf` | `192.168.70.130` |
| MySQL | `mysql` | `192.168.70.131` |
| AMF | `oai-amf` | `192.168.70.132`，N2/SCTP 38412 |
| SMF | `oai-smf` | `192.168.70.133`，N4/UDP 8805 |
| UPF | `oai-upf` | `192.168.70.134`，N3/UDP 2152 |
| External DN | `oai-ext-dn` | `192.168.70.135` |
| UDR / UDM / AUSF | `oai-udr` / `oai-udm` / `oai-ausf` | `.136` / `.137` / `.138` |
| IMS | `ims` | `192.168.70.139` |

`oai-cn5g_Android/conf/config.yaml` 修改要点：

- AMF GUAMI 和 PLMN support 统一为 `46692`、TAC `0x0001`。
- 切片为 SST `1`。
- DNN 为 `oai`、`openairinterface`、`ims`。
- IPv4 网段分别为 `10.0.0.0/24`、`10.0.1.0/24`、`10.0.9.0/24`。
- UPF 启用 SNAT。
- AMF 完整性算法为 NIA1/NIA2，加密算法为 NEA0/NEA1/NEA2。

启动：

```bash
cd '/home/usrp2/桌面/OAI(1)/oai-cn5g_Android'
docker compose up -d
```

## 3. 订阅数据库

`oai-cn5g_Android/database/oai_db.sql` 包含四个 `46692` 订阅，Ki/OPc 与 GRSP 一致。本日志不重复密钥明文。

| IMSI | `oai` DNN 静态 IP | 用途 |
| --- | --- | --- |
| `466920000000001` | `10.0.0.2` | 第一部手机 |
| `466920000000002` | `10.0.0.3` | 第二部手机备用 |
| `466920000000003` | `10.0.0.4` | 备用 |
| `466920000000004` | `10.0.0.5` | 备用 |

订阅还含 `ims` DNN。数据库中 session AMBR 为上/下行 1000 Mbps。

## 4. gNB 配置修改

- PLMN 改为 MCC `466`、MNC `92`。
- N2/N3 绑定 `oai-cn5g`，gNB 地址 `192.168.70.129/24`，AMF `192.168.70.132`。
- 使用 n78、30 kHz SCS、106 PRB、SA。
- X410 UHD 参数为 `addr=192.168.100.2,clock_source=internal,time_source=internal`。
- RU 当前 `nb_tx=1`、`nb_rx=1`。曾尝试 2 RX，但出现 UHD RX overflow/实时性问题，因此回退到 1 RX。
- `att_tx=0`映射为 60 dB TX gain；`att_rx=0`、`max_rxgain=40`映射为 40 dB RX gain。
- `pusch_TargetSNRx10=200`、`pucch_TargetSNRx10=200`。
- 放宽上行失步判定：`pusch_FailureThres=100`、`ulsch_max_frame_inactivity=10`。
- PRACH DTX 阈值为 `350`。
- 安全算法优先 `nea0` / `nia2`，DRB ciphering 开启，DRB integrity 关闭。

TDD 模式为 5 ms 周期：7 个 DL slot + 6 个 DL symbol + 2 个 UL slot + 4 个 UL symbol。

## 5. gNB 启动与 Web SoftScope

`oai-gnb-runner` 当前执行：

```bash
cd /home/usrp2/openairinterface5g/cmake_targets/ran_build/build
./nr-softmodem \
  -O '/home/usrp2/桌面/gnb1.sa.band78.fr1.106PRB.prs.usrpx310_oai.conf' \
  --sa --websrv \
  --websrv.listenaddr 127.0.0.1 \
  --websrv.listenport 8090
```

Web SoftScope 已编译，产物：

```text
/home/usrp2/openairinterface5g/cmake_targets/ran_build/build/libwebsrv.so
```

`libnrscope.so` 也存在，但网站直接消费 WebScope REST/WebSocket 数据，没有嵌入原生 UI。

## 6. 频点调试历史

1. 初始/备份频率为 3319.68 MHz（SSB `621312`，Point A `620040`）。
2. 曾在约 3400.32 MHz 调试。
3. X310 扫频在约 3408.96 MHz 看到商用网信号，因此将 OAI 移至 3629.28 MHz 作干扰对照。
4. 3629.28 MHz 上约 30 秒掉线仍曾复现，所以问题不是单一 3408.96 MHz 邻频干扰。
5. 网页后续将频率改为当前 **3459.36 MHz**（SSB `630624`，Point A `629352`）。

网页修改频率时会按 1.44 MHz SSB 同步栅格就近对齐，同时更新 ARFCN/Point A 并重启 gNB。

## 7. 天线与 X310 测量端

- X410 (`192.168.100.2`) 作为 gNB。
- X410 RF0 TX/RX 与 RF0 RX 物理上各接一根并排天线。
- 对面放置手机和喇叭天线。
- 喇叭天线连至 X310 (`192.168.40.2`) RF1 RX，用于测量手机位置的接收幅度。
- X310 是可移除的外置 RF 测量终端，不参与 gNB 协议栈。
- 虽然 X410 物理上接两根天线，当前软件 `nb_rx=1`，只处理一路 RX stream。

## 8. 约 30 秒掉线排查

已排查：

- 5GC 10 个服务稳定运行，未发现 AMF/SMF/UPF 整体崩溃。
- 更换频点后仍可复现，不是单一频点干扰。
- WebScope 开/关时都曾复现，不是 Scope 本身导致。
- 约 105 Mbps 连续下行流量时仍曾掉线，不是简单的无业务空闲超时。
- 2 RX 存在 UHD overflow，已回退 1 RX。
- 手机原允许多 RAT，后设为 NR-only。NR-only 后首轮 45 秒观察未掉线。

若问题再现，应对齐 UE/gNB/AMF 时间戳，定位 RRC Release/Radio Link Failure、NGAP UE Context Release 或 NAS deregistration 中的首个触发点。

## 9. 复查命令

```bash
docker compose -f '/home/usrp2/桌面/OAI(1)/oai-cn5g_Android/docker-compose.yaml' ps
docker inspect -f '{{.State.Status}} {{json .Config.Cmd}}' oai-gnb-runner
curl -s http://127.0.0.1:8787/api/status | python3 -m json.tool
```
