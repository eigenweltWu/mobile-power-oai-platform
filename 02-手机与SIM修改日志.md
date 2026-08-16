# 测试手机、SIM 与 Android 网络修改日志

> 日期：2026-08-13  
> 范围：USB/ADB/scrcpy、SIM GRSP、运营商显示、飞行模式、NR-only 和手机支持性排查。

## 1. 当前第一部手机状态

| 项目 | 当前值 |
| --- | --- |
| 型号 | Redmi K30i 5G |
| ADB 序列号 | `53616213` |
| SIM slot | `0` / SIM 1 |
| IMSI | `466920000000001` |
| PLMN | `46692` |
| 允许制式 | **NR only** |
| 实际 RAT | **NR_SA** (`RIL RAT 20`) |
| 注册 | PS/WWAN `HOME`，data `IN_SERVICE` |
| 频段 | n78 |
| 语音 | `OUT_OF_SERVICE`；当前无完整 IMS/VoNR |

NR-only 设置后连续约 45 秒、每 5 秒查询一次，均为 `NR` + `NR_SA` + `IN_SERVICE`，没有回落 LTE。

## 2. USB、ADB 和屏幕操作

- 测试手机通过 USB 连接本机。
- `scrcpy` 通过 ADB server TCP 端口 `5037` 共享屏幕。
- 用户已开启 Android 无障碍模式，允许自动进行屏幕操作。
- 两部手机曾同时通过 USB 连接。用户后续要求停止第二部手机配置，因此之后只操作 `53616213`。

## 3. GRSP 文件演变

| 文件 | IMSI / SPN | 状态 |
| --- | --- | --- |
| `oai_5gc_FR1.grsp` | `001010000000002` / OpenAirInterface | 原始 `00101` 示例，不适合当前 `46692` 5GC |
| `oai_5gc_redmi.grsp` | LTE IMSI `466920000000002` / OpenAirInterface | 过渡文件；GSM/CDMA 段仍有 `00101` 残留 |
| `oai_5gc_chunghwa_46692.grsp` | `466920000000001` / Chunghwa Telecom | 第一部手机的中华电信版 |
| `oai_5gc_chunghwa_46692_phone2.grsp` | `466920000000002` / Chunghwa Telecom | 第二部手机备用，后续停止配置 |
| `oai_5gc_xjtlu_46692.grsp` | `466920000000001` / XJTLU | 为自定义 SPN XJTLU 生成 |

XJTLU GRSP 的主要非密钥字段：

- ICCID：`89886920000000000003`
- IMSI：`466920000000001`
- HPLMN/EHPLMN：`46692`
- LTE PLMN/OPLMN/HPLMN：`46692:4000; 46692:8000; 46692:0080`
- SPN：`XJTLU`
- ACC：`0001`
- 卡类型：`LTE(TB01):LTE+GSM`
- MSISDN：空

Ki、OP/OPc 和 ADM 是鉴权密钥，已写入 GRSP 并与 5GC 数据库保持一致；本日志不重复明文。

## 4. “手机号码未知”的判断

Android 设置页显示“手机号码：未知”，通常表示 SIM 中 EF-MSISDN 没有写入。当前 GRSP 的 MSISDN 确实为空。

这不等于：

- 手机读不到 SIM。
- 手机不支持 5G。
- SIM 无法进行 5G AKA。

第一部手机已成功以 NR-SA n78 注册到 `46692`，因此可确认 Redmi K30i 5G 支持当前 OAI 网络。

## 5. 运营商名显示

项目曾先生成 SPN `Chunghwa Telecom` 的 GRSP，后按要求生成 SPN `XJTLU` 的 GRSP。

Android `ServiceState` 在不同时刻曾显示 `XJTLU`，最后一次现场查询又显示 `Chunghwa Telecom`。这表明当前实卡 EF-SPN 或 Android 运营商缓存仍可能保留中华电信名称。“已生成 XJTLU GRSP”不等于能证明每次读取都已持久化为 XJTLU。

## 6. 飞行模式与无线电重置

为重新读取 SIM/制式配置并发起注册，曾使用：

```bash
adb -s 53616213 shell cmd connectivity airplane-mode enable
adb -s 53616213 shell cmd connectivity airplane-mode disable
```

用户也曾手动为第一部手机开启再关闭飞行模式。

## 7. 设置 5G NR-only

修改前的权威查询结果：

```text
GPRS|EDGE|UMTS|HSDPA|HSUPA|HSPA|LTE|HSPA+|GSM|TD_SCDMA|LTE_CA|NR
```

因此当时不是 NR-only。

首次将十进制 `524288` 传给命令时，系统返回 `No valid NETWORK_TYPES_BITMASK`，该次尝试未改变手机。

根据该 Android 实现的 `cmd phone help`，NR-only 需使用 20 bit 二进制字符串：

```bash
adb -s 53616213 shell \
  'cmd phone set-allowed-network-types-for-users -s 0 10000000000000000000'
```

设置后执行飞行模式开/关，并查询：

```bash
adb -s 53616213 shell \
  'cmd phone get-allowed-network-types-for-users -s 0'
```

返回仅：

```text
NR
```

随后从 `telephony.registry` 确认实际数据注册：

```text
gsm.network.type = NR_SA,Unknown
mDataRegState = 0 (IN_SERVICE)
getRilDataRadioTechnology = 20 (NR_SA)
PS/WWAN registrationState = HOME
band = 78
PLMN = 46692
```

该操作 **没有重写 SIM、没有改 PLMN**，只改了 Android SIM 1 的用户允许制式掩码并重启无线电。

## 8. 语音与数据状态

当前可同时看到：

- Voice registration：`OUT_OF_SERVICE`。
- Data registration：`IN_SERVICE` / `NR_SA`。

原因是当前实验网未完成 IMS/VoNR 注册和语音业务验证。语音未注册不能被当作 5G 数据掉线。

## 9. 第二部手机

- 为其生成了独立 IMSI `466920000000002` 和 ICCID 的 GRSP。
- 它曾出现无网、不显示手机号码。后者只说明 EF-MSISDN 为空，不能证明型号不支持。
- 用户后续明确停止第二部手机配置，因此未继续改其 RAT、APN 或 SIM。
- 因缺少确切型号和 NR SA/n78 capability 结果，无法对第二部手机做最终硬件支持性结论。

## 10. 复查命令

```bash
adb devices
adb -s 53616213 shell \
  'cmd phone get-allowed-network-types-for-users -s 0'
adb -s 53616213 shell getprop gsm.network.type
adb -s 53616213 shell getprop gsm.operator.numeric
adb -s 53616213 shell dumpsys telephony.registry
```

系统升级、恢复网络设置、换卡或厂商电话服务重置后，应重新查询 allowed network types，不应假设 NR-only 永久不变。
