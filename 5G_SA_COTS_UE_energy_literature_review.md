# 商用 5G SA 智能手机能耗：跨层建模、因果干预与传播环境泛化的系统性批判综述

**检索截止日期：2026-08-14**  
**重点时间范围：2020–2026；保留 2010–2019 年基础工作**

## 执行摘要

### 最直接的判断

这项研究**有较强的高水平论文潜力，但不应以“又一个 5G 手机功耗预测模型”为主线**。现有工作已经充分覆盖了以下命题：

- 手机/UE 功耗与吞吐、RRC/DRX 状态、信号强度和 UE 发射功率相关；
- LTE 时代已经有把 PRB、MCS、信道、业务和资源分配纳入 UE 功耗模型的工作；
- 5G 时代已有 COTS 手机的 UE-only 决策树模型、商用 5G 模块的发射功率/带宽模型，以及面向 UE power saving 的网络协同方案；
- 3GPP、O-RAN 和产业界已经明确认可“网络辅助 UE 节能”和“per-UE energy information”具有部署意义。

真正稀缺、且与现有工作形成清晰正交关系的是：

1. **因果干预**：在下行覆盖近似不变时，从 gNB 改变 PUSCH target SNR，观察 `TPC → PH/Tx regime → battery power`；
2. **跨层 oracle 与可部署模型的严格比较**：同一批真实手机数据上定量回答 gNB 变量究竟带来多少增益；
3. **物理场与协议测量联结**：同时获得 DUT 位置的校准入射场、手机 SS-RSRP/SS-SINR、gNB 端调度/功控状态和整机电功耗；
4. **传播环境外推**：AC 内训练、RC 外测，而不是同一场景随机切分；
5. **机制模型与部署模型分离**：gNB 变量用于干预、解释、教师/oracle 和机制验证，最终 student 只使用普通 Android 可得变量。

因此，本报告推荐 **Position B：Cross-layer causal characterization + UE-only deployable estimator**。Position C 可作为条件性第二主线；Position A 单独成文的风险最大。

---

# 1. 最重要的结论

## 1.1 最大的已有工作重叠

### 重叠一：发射功率、PRB、资源分配与 UE 功耗

LTE 时代的 Jensen/Lauridsen 模型和 CoPoMo 已经表明，UE 功耗可以由 RRC 状态、UL/DL 速率、接收功率、发射功率、带宽、PRB 和业务上下文表征。Jensen 等人的线性组件模型在其测试条件下平均验证误差为 **2.6%**；CoPoMo 更明确把 PRB、MCS、信道/位置、业务和资源分配放进面向运营商分析的随机模型。因此，“首次将无线参数加入手机功耗模型”不能成立。[Jensen et al.](https://doi.org/10.1109/VTCFall.2012.6399281)；[CoPoMo 全文](https://onlinelibrary.wiley.com/doi/full/10.1002/ett.2702)

### 重叠二：真实 5G 手机的 UE-only 功耗预测

Narayanan 等人在 SIGCOMM 2021 已针对不同手机、运营商和 5G 配置分别训练决策树回归模型，输入为吞吐和信号强度；其消融显示两者结合始终优于仅吞吐或仅信号，真实应用验证平均相对误差为视频 **3.7%**、网页 **2.1%**。因此，“RSRP/SINR/throughput 预测 5G 手机功耗”本身不新。[Narayanan et al. 全文](https://conferences.sigcomm.org/sigcomm/2021/files/papers/3452296.3472923.pdf)

### 重叠三：5G 多特征/机器学习 UE 功耗模型

2024 年 Jopanya 硕士论文在 5G mmWave NSA 条件下比较了 polynomial regression、decision tree 和 neural network，并报告加入少量高相关特征可改善预测；虽然其不是同行评审论文，但会成为审稿人用于否定“多特征 ML 新颖性”的直接反例。[论文元数据与摘要](https://www.diva-portal.org/smash/record.jsf?pid=diva2%3A1911308)

### 重叠四：网络辅助 UE 节能

Samsung Research 的 Ali 等人使用商用网络和商用 UE，先在 UE 端进行业务分类，再通过 3GPP Release 16 UE Assistance Information 请求合适的带宽/MIMO 层数配置；其报告 cell-center 最多节能 **60%**、cell-edge 最多 **50%**，且不降低 QoS。这表明“网络侧具有 UE 节能部署场景”并非假设，而是已有标准化路径。[Ali et al., IEEE Access 2023](https://doaj.org/article/f53ad700aadf4d62893395535da6ba15)

## 1.2 最大的新颖性机会

### 机会一：真正的闭环功控因果实验

本轮检索找到的最强邻近工作是 NIST TN 2147：它在受控 LTE 环境中研究 eNB 闭环功控、调度、负功率余量、UE 自报 Tx power 与实测 EIRP 的关系；但因变量是**辐射发射/每 PRB 功率**，不是手机电池或整机电功耗。[NIST TN 2147](https://www.nist.gov/publications/characterizing-lte-user-equipment-emissions-under-closed-loop-power-control)

OAI 文档则明确给出你们可用的干预机制：gNB 追踪 PUSCH/PUCCH SNR/RSSI，并通过 TPC 把 UE 维持在 `pusch_TargetSNRx10`；normal 与 deltaMCS 模式还会改变 MCS 在功控中的作用。[OAI MAC/power-control documentation](https://github.com/OPENAIRINTERFACE/openairinterface5g/blob/develop/doc/MAC/mac-usage.md)

在检索到的同行评审论文、技术报告、学位论文和标准文档中，**尚未发现**同时满足以下条件的直接先例：

- 真实 COTS 5G SA 智能手机；
- OTA 链路；
- 下行 RSRP/入射场近似固定；
- 主动改变 gNB PUSCH target SNR；
- 同步观测 TPC、PH/PHR、PUSCH SNR/RSSI、PRB/MCS/HARQ；
- 以整机电功耗/电池电流为因变量。

这不是“已证明首次”，而是一个**有强邻近证据、但本轮检索未找到直接工作**的候选空白。

### 机会二：AC 与 RC 的 matched-condition 反事实比较

RC 文献大量研究 TRP、TIS、MIMO OTA、吞吐、灵敏度和基站功能；AC 文献大量研究天线、辐射、EMF 和测试校准。本轮检索未找到把 AC/LOS 与 RC/rich-multipath 在**相同平均入射场或相同手机 RSRP**下进行 COTS 手机电功耗比较，并同步记录 HARQ/MCS/CQI/RI/Tx behavior 的论文。现有 RC 标准和实验恰好说明该环境差异是可校准、可重复的，但能耗端点尚未成为主流。[3GPP RC/OTA TR 37.977](https://www.etsi.org/deliver/etsi_tr/137900_137999/137977/13.04.00_60/tr_137977v130400p.pdf)；[5G system in RC](https://arxiv.org/abs/2302.14710)

### 机会三：把 gNB 特征的价值变成一个可证伪问题

现有文献通常只做其中一类模型：

- UE-only：吞吐 + 信号强度；
- test-instrument model：直接设定 Tx power、带宽、PRB/MCS；
- standardized relative model：根据 PDCCH/PDSCH/UL/sleep 状态赋相对能耗；
- RAN optimization：使用理论 UE 能耗模型求解资源分配。

几乎没有同一真实手机/同一实验集上严格比较 (M_1,M_2,M_3) 的工作。因此，“gNB 变量有没有增益”本身就是一个有价值、可能得到否定结果的研究问题。

## 1.3 最大的 significance 风险

1. **部署错位**：普通 App 不可见 TPC、PH、PRB、HARQ、MCS 和 scheduler state。若论文只给出 (P=f(X_{gNB}))，用户侧不可部署；若又没有闭环 RAN 优化，模型会被质疑“知道得多但无用途”。
2. **增量信息不足**：gNB 特征可能只是 UE Tx power、traffic load 和 link quality 的代理；RSRP/SINR/throughput/traffic 已捕获大部分方差后，ΔMAE 可能很小。
3. **整机标签不干净**：Android `CURRENT_NOW` 来自 fuel gauge，采样、符号、滤波和实现依设备而异；CPU、屏幕、温控、后台任务和充放电状态都会污染 modem 因果效应。
4. **干预不只改变 Tx power**：target SNR 会同时改变 TPC、MCS、BLER、HARQ、PRB、goodput 和传输时长。若不做固定 offered load/固定数据量和中介分析，不能把总效应简单解释成“Tx power 导致功耗”。
5. **AC/RC 匹配不充分**：仅匹配平均 RSRP 不足以匹配 RSRQ/SINR 分布、时延扩展、相干时间、极化、MIMO rank、stirrer 状态和 UE 自适应窗口。
6. **设备泛化**：PA 架构、调制解调器、天线位置、散热和 Android fuel gauge 差异很大。单一手机容易被判为 device-specific profiling。

## 1.4 应放弃或降级的方向

- 放弃把“弱信号导致更耗电”作为主结论；
- 放弃 4G/5G 比较，不仅因为平台限制，也因为已有工作很多；
- 放弃仅在同一数据集随机切分后追求极低 RMSE；
- 放弃把“使用 Random Forest/XGBoost/NN”本身写成创新；
- 放弃只做 (E=f(RSRP)) 的单变量拟合；
- 降级“gNB-side estimator”作为唯一贡献，除非实现并评估真正的 scheduler/power-control 闭环；
- 不应把校准入射功率密度单独包装成网络论文贡献；它必须服务于“为什么同一 UE 指标在不同物理场中映射到不同能耗机制”。

## 1.5 建议作为论文核心的内容

1. **受控因果链**：`target SNR → TPC → PH/PCMAX saturation → UE transmit regime → phone power/energy per bit`；
2. **跨环境机制**：在 AC 与 RC 中匹配平均场/RSRP，检验 link adaptation、HARQ 和 rank 是否造成不同能耗；
3. **三层模型与严格消融**：UE-minimal、UE-rich、cross-layer oracle；
4. **OOD 泛化**：leave-run/day/orientation/chamber/device-out，而非随机样本切分；
5. **teacher/oracle → student**：gNB 特征仅训练期可见，部署期只用 Android 公共 API；
6. **结果驱动的双出口**：若 gNB 增益小，主打 causal explanation；若增益大且稳定，进一步实现 RAN-side estimator/energy-aware scheduling。

---

# 2. 检索方法与证据等级

## 2.1 检索策略

本综述使用了多组交叉关键词，而非只检索题名。关键词簇覆盖：

- smartphone/UE/modem power model、battery drain、energy per bit；
- RSRP/SINR/throughput/Tx power/MCS/PRB/HARQ/TPC/PHR/scheduler；
- network-assisted、RAN-side、energy-aware scheduler、UE assistance；
- PUSCH target SNR、closed-loop power control、controlled intervention；
- anechoic/reverberation chamber、OTA、incident power density、orientation、LOS/NLOS；
- cross-environment/domain generalization/OOD；
- privileged information/LUPI/teacher-student/distillation；
- OAI/Open5GS/srsRAN/O-RAN/private 5G/COTS phone。

交叉核验渠道包括出版社/会议全文页、ACM/IEEE 元数据、DOI/Crossref、DBLP、大学机构库、NIST、3GPP/ETSI、OAI 官方文档和作者公开稿。对高度相关工作进行了参考文献和后续工作追踪。Semantic Scholar API 在本次会话中持续返回 HTTP 429，因此未把其引用计数用于论证；这不影响论文内容核验，但投稿前仍应在 IEEE Xplore、ACM DL、Scopus/Web of Science 中补做一次正式 citation report。

## 2.2 证据标记

- **强证据**：已阅读全文或官方标准/官方 API 文档；
- **中等证据**：出版社摘要、作者稿的可检索全文片段或学位论文全文索引；
- **未发现**：多关键词、多类别来源检索未出现直接工作；不等价于不存在；
- **待验证**：投稿前应以数据库 citation chaining、作者联系或付费全文复核。

## 2.3 支撑“未发现直接工作”的代表性检索组合

以下组合分别以普通网页学术索引、出版社页面、机构库、官方标准库和全文片段交叉检索；结果不是只依据标题：

- `"PUSCH target SNR" smartphone power consumption`、`"target SNR" PUSCH battery`、`closed-loop uplink power control handset energy`：命中 OAI/Amarisoft 文档、NIST LTE emissions、功控算法与专利，但未命中 5G SA 手机电功耗干预实验；
- `anechoic chamber smartphone 5G power consumption`、`calibrated incident power density smartphone RSRP energy`：命中 OTA/EMF/IPD/天线与 network-characterization 工作，但未命中“校准入射场 + phone RSRP + electrical power”三联测量；
- `reverberation chamber smartphone power consumption 5G`、`rich multipath UE energy HARQ MCS`、`same average RSRP LOS multipath battery`：命中 RC throughput/TRP/TIS、RedCap channel-emulator 和 LOS/NLOS module-energy 工作，但未命中 matched-condition phone-energy 对比；
- `gNB UE battery consumption prediction PRB MCS HARQ TPC PHR`、`RAN-side smartphone power model`：命中 CoPoMo、链路/BER prediction、3GPP per-UE network-energy accounting 与 UE Assistance，但未命中完整 5G phone power predictor；
- `cross-environment smartphone power prediction cellular`、`AC train RC test UE energy`、`domain generalization wireless energy model`：命中一般无线 DG/信号预测与感知工作，未命中 smartphone-energy AC→RC 验证；
- `privileged information gNB telemetry smartphone energy`、`train with network telemetry deploy with UE telemetry`：命中一般 LUPI/teacher–student 与邻域通信应用，未命中该目标组合。

“未命中”的结论还通过已找到论文的 related work/reference list 进行反向核对；但由于厂商内部研究、未索引会议稿和最新专利可能存在，仍保留“待投稿前数据库复核”的限定。

---

# 3. 文献分类表

> 表中“network variables”区分了**实际 gNB telemetry**、**测试仪直接控制参数**和**仿真配置**；三者不能混同。为便于阅读，变量仅列最关键项。

| Paper | Year/Venue | UE/Device | 4G/5G | SA/NSA | Environment | Energy measurement | UE-side variables | gNB/network variables | Controlled intervention | Prediction model | Main result | Gap relative to us |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| [Huang et al., A Close Examination of Performance and Power Characteristics of 4G LTE Networks](https://web.eecs.umich.edu/~zmao/Papers/RRC4G_mobisys2012.pdf) | 2012 / ACM MobiSys | LTE phone | 4G | LTE | Commercial/operator + lab traffic | External power monitor | traffic, UL/DL rate, RRC state | inferred RRC timers | traffic pattern | state/linear empirical | (P=\alpha_uR_u+\alpha_dR_d+\beta); tail/DRX dominant | No 5G, no PHY telemetry, no controlled RF |
| [Jensen et al., LTE UE Power Consumption Model](https://doi.org/10.1109/VTCFall.2012.6399281) | 2012 / IEEE VTC | commercial LTE USB dongle | 4G | LTE | Agilent network emulator, conducted | external supply/current | Tx/Rx power, UL/DL rate, RRC | emulator settings | Tx/Rx power, rates | component-wise linear regression | mean verification error 2.6%; UL Tx power and DL data rate important | not phone/OTA; no HARQ/TPC/PH traces |
| [Noël/Lauridsen et al., Empirical LTE Smartphone Power Model with DRX](https://vbn.aau.dk/ws/files/194890384/UEpowerModel2013.pdf) | 2013 / IEEE VTC | second-generation LTE smartphone | 4G | LTE | network emulator | battery dummy/external meter | Tx/Rx power, rates, DRX | bandwidth, emulator configs | DRX/rates/power | empirical state model | extends model with DRX and cell bandwidth | LTE, tester, no deployment ablation |
| [Dusza et al., Accurate Measurement-Based Power Model](https://scispace.com/pdf/an-accurate-measurement-based-power-consumption-model-for-2kiksyl7ei.pdf) | 2013 / IEEE INFOCOM Poster | 4 data sticks + 1 smartphone | 4G | LTE | BS emulator | measurement probe | device/frequency | controlled UL Tx power | direct Tx-power sweep | piecewise empirical | nonlinear PA regime; device-specific threshold | no live gNB telemetry; LTE |
| [Dusza et al., CoPoMo](https://onlinelibrary.wiley.com/doi/full/10.1002/ett.2702) | 2013 / Trans. Emerging Telecomm. Tech. | HTC/Galaxy phones + LTE sticks | 4G | LTE | tester + ray-tracing/system simulation | external probe | service, channel/location, traffic | PRB, MCS/resource allocation, Tx power | tester/system configs | piecewise + Markov/context model | enables battery lifetime/resource-allocation trade-off | strongest classic overlap; no 5G SA, HARQ/TPC/PH, AC/RC |
| [Lauridsen et al., Empirical LTE Smartphone Power Model](https://vbn.aau.dk/da/publications/an-empirical-lte-smartphone-power-model-with-a-view-to-energy-eff) | 2014 / Intel Technology Journal | LTE smartphones | 4G | LTE | network emulator | external | Tx/Rx power, rates, DRX | bandwidth/configs | controlled | empirical component model | Tx >10 dBm and subsystem-on power are major components | no 5G/OTA/cross-env |
| [Ding et al., Signal Strength and Smartphone Battery Drain](https://doi.org/10.1145/2494232.2466586) | 2013 / ACM SIGMETRICS PER | 3,785 phones | 3G/Wi-Fi | n/a | crowdsourced real world | battery telemetry | signal strength, usage | none | no | empirical/statistical | signal strength improves prior battery-drain models | correlation only; legacy RAT; no gNB |
| [Falkenberg et al., ML Uplink Tx-Power Prediction](https://arxiv.org/abs/1806.06620) | 2018 / IEEE VTC | LTE modem/drive-test system | 4G | LTE | field/passive DL indicators | target is UE RF Tx power, not electrical power | passive DL indicators | no privileged gNB inputs | observational | ML regression | predicts Tx power from passive DL information | bridge only; endpoint is Tx dBm |
| [Lauridsen et al., 5G NR UE Power Modeling and Potential Energy Savings](https://doi.org/10.1109/VTCFall.2019.8891215) | 2019 / IEEE VTC | numerical UE | 5G | NR model | simulation | 3GPP relative units | power states/traffic | PDCCH/DRX/scheduling configs | simulated | standardized state model | short DRX saves 26%; PDCCH optimizations additional 17–20% in studied setup | not measured COTS power; coarse relative model |
| [3GPP TR 38.840 UE Power Saving in NR](https://atisorg.s3.amazonaws.com/archive/3gpp-documents/Rel16/ATIS.3GPP.38.840.V1600.pdf) | 2019 / 3GPP Rel-16 | reference UE | 5G | NR | simulation methodology | relative power: sleep/PDCCH/PDSCH/UL | state/activity | scheduling/DRX/BWP/antennas | simulated | lookup/state model | FR1 UL relative power 250 at 0 dBm, 700 at 23 dBm | not device-calibrated; intermediate Tx powers incomplete; baseline assumes no HARQ |
| [Xu et al., Understanding Operational 5G](https://cs.nyu.edu/~anirudh/CSCI-GA.2620-001/papers/operational_5g.pdf) | 2020 / ACM SIGCOMM | ZTE Axon 10 Pro; Huawei Mate 20X/30 Pro | 4G/5G | NSA | commercial networks, 7 months | Android kernel current/voltage (`pwrStrip`) | RSRP/RSRQ/SINR, traffic/apps | XCAL diagnostic CQI/MCS/PCI | selected locations/traffic | profiling, state analysis | 5G modem 55.18% average measured budget; 5G tail ≈20 s; switch simulation saves 24.8% | proprietary diagnostic, commercial scheduling, no causal gNB control |
| [Narayanan et al., A Variegated Look at 5G in the Wild](https://conferences.sigcomm.org/sigcomm/2021/files/papers/3452296.3472923.pdf) | 2021 / ACM SIGCOMM | Pixel 5; Galaxy S20 Ultra/S10 5G | 4G/5G | SA/NSA | 3 US operators, mobility/mmWave/sub-6 | Monsoon + Android/rooted tools | throughput, signal strength, app state | none | location/app/traffic variations | per-setting decision-tree regression | TH+SS beats either alone; video/web error 3.7%/2.1% | no gNB variables; separate models; no chamber/OOD device test |
| [NIST TN 2147, Closed-Loop Power Control](https://nvlpubs.nist.gov/nistpubs/TechnicalNotes/NIST.TN.2147.pdf) | 2021 / NIST | commercial LTE UE | 4G | LTE | controlled radiated/shielded lab | EIRP/emissions, not battery | UE-reported Tx power | path loss, scheduling, closed-loop PC, negative PH | yes | statistical emissions models | quantifies reported Tx vs EIRP and scheduling under negative PH | closest causal mechanism, wrong RAT and endpoint |
| [Pu & Wu, mmWave Smartphone Power/Thermal Model](https://arxiv.org/abs/2304.06512) | 2023 / preprint | Pixel 5 | 5G | commercial NSA/mmWave context | Verizon/T-Mobile, thermal tests | total power; skin/ambient temp | DL/UL throughput, CPU use/freq, band/channel | none | workload/temp | multivariable linear + thermal model | validation RMSE 880.82 mW, reported accuracy 83.4%; transceiver dominates throttling | no RF/gNB variables, no SA/controlled channel |
| [Ali et al., UE Power Saving with Traffic Classification and UE Assistance](https://doaj.org/article/f53ad700aadf4d62893395535da6ba15) | 2023 / IEEE Access | commercial UEs | 5G | commercial network | commercial field data | UE power in evaluation | traffic class, link condition | network applies requested BW/MIMO layers | configuration adaptation | XGBoost traffic classifier + lookup/optimization | up to 60%/50% saving center/edge without QoS loss | optimization, not power prediction; UE asks network |
| [Schippers & Wietfeld, Data-Driven Energy Profiling](https://comnets.etit.tu-dortmund.de/storages/cni-etit/r/Research/Publications/2024/Schippers_2024_CCNC/Schippers_CCNC2024_AuthorsVersion.pdf) | 2024 / IEEE CCNC | Quectel RM500Q/RM520N modules | 4G/5G | SA/NSA | R&S tester + lab + field map | external DC supply/meter | passive radio/trajectory features | controlled Tx power, BW, PRB, MCS, band, TDD | yes, tester | piecewise electrical model + RF Tx-power prediction | MCS/PRB influence ruled out in tested setup; Tx-power RF prediction RMSE 4 dB; maps show up to 5× battery-life variation | not phone; no gNB telemetry; direct counterexample that PRB/MCS may add little |
| [Jopanya, Power Consumption Modeling of 5G mmWave UE](https://www.diva-portal.org/smash/record.jsf?pid=diva2%3A1911308) | 2024 / Linköping MSc | mmWave UE/test dataset | 5G | NSA | controlled experiments | UE power | correlated UE/system features | MCS/network configs in dataset | several test schemes | polynomial, DTR, NN | DTR/NN versatile; adding correlated features improves prediction | thesis, NSA/mmWave, no phone app/AC-RC/causality |
| [Staal, Measuring Power of Virtualized 5G with OAI](https://essay.utwente.nl/fileshare/file/105180/staal_MA_EEMCS.pdf) | 2025 / Twente MSc | OAI UE or Quectel RM500U | 5G | SA | OAI + conducted RF/attenuators | Keysight N6705B | throughput/module state | bandwidth, DL MCS, TDD, OAI config | yes | profiling | bandwidth/throughput/MCS alter module power | closest OAI-SA work; no smartphone, OTA, prediction or chambers |
| [Jörke et al., 5G NR vs RedCap Devices](https://cni.etit.tu-dortmund.de/storages/cni-etit/r/Research/Publications/2025/Joerke_2025_CCNC/Joerke_CCNC2025_AuthorsVersion.pdf) | 2025 / IEEE CCNC | off-the-shelf NR/RedCap modems | 5G | SA/tester context | HIL channel emulator, multipath/fading | external power | signal/traffic/device state | channel/emulator, RRC/DRX | yes | empirical profiling | RedCap up to 2× battery life; 80 Mbit/s under fading | not smartphone; focuses RedCap/state, not cross-layer prediction |
| [Danger et al., Context-Sensitive IRS Support](https://comnets.etit.tu-dortmund.de/storages/cni-etit/r/Research/Publications/2025/Danger_2025_PIMRC/Danger_PIMRC2025_AuthorsVersion.pdf) | 2025 / IEEE PIMRC | commercial mmWave module | 5G | commercial/private setting | robotic orientation/mobility LOS/NLOS/BLOS | external device power | SS-RSRP, Tx power, throughput, beam/context | network/IRS condition | orientation/path intervention | energy profile | 19.9% lower power, 29.1% higher EE in reported optimized case | strong orientation counterexample; no phone/AC-RC/gNB causal chain |
| [Rahman et al., Smartphone Data-Assisted BER Prediction](https://doi.org/10.1109/MILCOM64451.2025.11310026) | 2025 / IEEE MILCOM | COTS smartphone | 4G | LTE | controlled indoor nLOS | no UE electrical energy; predicts BER | CQI/SNR | MCS, RB, PHR | channel condition | Ridge/SVR/KNN | shows PHR/RB/MCS useful for link-quality prediction | important adjacent cross-layer use, but target is BER, not power |
| [3GPP TR 26.942, Media Energy Framework](https://www.etsi.org/deliver/etsi_tr/126900_126999/126942/19.00.00_60/tr_126942v190000p.pdf) | 2026 / 3GPP Rel-19 study | generic UE/app | 5G | 5GS | architecture/standards | UE/app battery info or network-node EC allocation | application battery/energy info | EIF/OAM/gNB/UPF EC and data volume | no | accounting/exposure framework | considers per-UE/app reporting, UE privacy and energy abstraction; notes APIs need further study | proves deployment relevance, but not PHY-telemetry inference of whole-phone power |

## 3.1 从表中可得出的 RQ1 答案：模型通常需要什么输入？

### 物理/组件模型

常见输入：

- UE Tx power；
- Rx power/信号强度；
- UL/DL throughput 或 offered load；
- RRC/DRX/sleep/active state；
- bandwidth、carrier、MIMO chains/layers；
- baseband/CPU activity；
- 设备特定的 PA 分段参数和固定功耗。

优点是可解释，缺点是设备依赖、状态测量困难、在现代手机上组件隔离困难。

### empirical regression

常见输入：Tx power、throughput、signal strength、traffic、band/channel、temperature、CPU use。输出通常是瞬时功率、平均功率或每任务能量。线性模型在受控单一机制下很好，但遇到 PA mode、carrier aggregation、thermal throttling、DRX 和调度离散状态时会失配。

### machine learning

常见输入并不比 empirical model 更“底层”：通常仍是 throughput、signal strength、traffic/application、CPU/thermal、band/device/carrier。DTR/RF/XGBoost/NN 的优势主要是拟合非线性和离散状态，而不是自动获得不可见的 gNB 特征。

### 标准/系统级相对模型

3GPP TR 38.840 主要输入是 UE 所处的 sleep、PDCCH-only、PDCCH+PDSCH、UL 等活动状态和少数配置；UL 只明确给出 0 dBm/23 dBm 参考点，中间功率需要插值或厂商自报模型，且基本校准场景假设首次解码成功、无 HARQ 重传。这类模型适合协议节能比较，不适合预测某台真实手机的绝对整机功耗。[TR 38.840 FR1 model](https://itecspec.com/3gpp/38.840/s/8.1.1)；[TR 38.838 对 UL 模型不足的说明](https://itecspec.com/3gpp/38.838/s/a.2)

## 3.2 是否已有论文使用 PRB、MCS、HARQ、TPC、PH/PHR、CQI、scheduler state 预测真实商用手机功耗？

**分项回答：**

- **PRB/MCS**：有。CoPoMo 把 PRB/MCS/资源分配纳入 LTE 上下文模型；Schippers 2024 和 Staal 2025 在 5G 模块实验中控制/考察 PRB、MCS、带宽。但 Schippers 明确报告，在其测试设置中选择的 MCS 和 PRB 数没有显著影响，最终核心模型主要依赖 Tx power 和 bandwidth。
- **CQI/MCS 作为诊断变量**：Xu 2020 通过 XCAL 获取 CQI/MCS，但并未把完整 gNB 特征集用于整机功耗预测；Rahman 2025 用 CQI/MCS/RB/PHR 预测 BER，不预测电功耗。
- **TPC、PH/PHR、HARQ rounds、scheduler state 的联合功耗模型**：本轮检索**未找到**把这些实际 gNB telemetry 联合用于预测 COTS 5G SA 智能手机整机电功耗，并与 UE-only 模型进行消融的论文。
- **NIST**：使用闭环功控、负 PH 和 scheduler dynamics，但输出是 RF emissions/EIRP，不是电功耗。

所以最精确的结论是：**这些变量分别在 UE 功控、链路质量预测、资源分配和系统级能耗模型中出现过；但“联合真实 gNB trace → COTS 5G SA 手机电功耗”的直接实证证据仍有限。**

---

# 4. gNB-side information 的实际意义

## 4.1 RQ A：是否存在 (P_{UE}=f(MCS,PRB,TPC,HARQ,CQI,traffic,...))？

### 严格意义上的答案

对**真实 COTS 5G 智能手机整机电功耗**，本轮未找到完全对应的同行评审模型。

对较宽松的定义，存在三类近似：

1. **LTE 上下文/资源模型**：CoPoMo 把 PRB、MCS、信道、业务、Tx power 连接到 UE 功耗；
2. **5G 模块模型**：Schippers 2024、Jopanya 2024、Staal 2025 研究 Tx power、带宽、MCS/PRB/吞吐与 UE/module power；
3. **理论/标准模型**：3GPP 相对状态模型和大量 energy-aware RRM 论文用调度状态、带宽、Tx power、DRX/BWP/MIMO layers 计算“UE energy”，但不是基于真实手机 battery-current 标签。

## 4.2 RQ B：这些模型用于什么？

已有或明确提出的用途包括：

- PRB/resource allocation 与 battery-lifetime trade-off（CoPoMo）；
- energy-aware scheduling、站点选择、部署规划（Schippers 2024）；
- DRX/PDCCH/BWP/MIMO layer adaptation（3GPP TR 38.840；Ali 2023）；
- cell/small-cell discovery、handover/offloading；
- private 5G 中面向机器人、传感器、视频业务的能效规划；
- O-RAN xApp/rApp 的策略输入；
- UE/application energy reporting、NWDAF/AF analytics（3GPP TR 26.942）；
- uplink power-control target 或 duty-cycle 优化。

但必须区分：很多论文优化的是**网络能耗**、理论 UE transmit energy 或模块功耗，而不是实际整机 battery drain。

## 4.3 RQ C：gNB 已有这些参数，是否有明确部署场景？

**是，但部署主体是 RAN，不是普通 App。**

gNB 原生拥有调度、PRB、MCS、HARQ、TPC、PHR/CQI 等信息，因此一个 gNB-side model 可部署为：

- OAI scheduler 内部的代价函数；
- Near-RT RIC xApp 的 per-UE energy KPI；
- Non-RT RIC/rApp 的设备类型和策略学习；
- private 5G 的业务准入、功控、BWP/DRX/MIMO-layer/traffic shaping；
- UE Assistance 或 energy/QoE reporting 的网络端补充模型。

3GPP Rel-19 的 TR 26.942 甚至讨论 UE/app 能耗报告、Data Collection AF、NWDAF exposure 以及 UE energy information abstraction，并明确指出原始 UE 能耗可能属于厂商/芯片商不愿共享的私有信息。[3GPP TR 26.942](https://www.etsi.org/deliver/etsi_tr/126900_126999/126942/19.00.00_60/tr_126942v190000p.pdf)

不过，这并不自动证明你们的模型有 significance：

- gNB 能估计的首先是**radio-related incremental power**，不一定是整机功耗；
- 设备型号、PA、热状态、SOC 和后台负载决定模型是否可迁移；
- 若没有可执行的控制动作，per-UE energy estimate 只是一个漂亮 KPI；
- 若 scheduler 改变后造成更多空口占用或其他 UE 受损，单 UE 节能不等于系统最优。

---

# 5. UE-only vs gNB-assisted：目前证据与建议实验

## 5.1 现有直接比较证据

### 有的比较

Narayanan 2021 比较：

\[
P=f(throughput,signal\ strength),\quad
P=f(throughput),\quad
P=f(signal\ strength)
\]

组合模型始终更好，尤其 signal-only 在 mmWave 下较差；但论文图中给出 MAPE 对比而不是可复用的数值表，也没有加入 PRB/MCS/HARQ/TPC。

Schippers 2024 的受控实验提供重要反证：在其商用 5G 模块和测试配置下，额外测量“排除了所选 MCS 与 PRB 数的影响”，说明网络侧变量不必然带来显著功耗预测增益。

### 缺失的比较

本轮没有找到如下完整消融：

\[
M_1=f(RSRP)
\]

\[
M_2=f(RSRP,SINR,throughput,traffic,temperature)
\]

\[
M_3=f(X_{UE},TPC,PH,PRB,MCS,HARQ,CQI,RI)
\]

并同时报告随机内测、unseen run、unseen environment、unseen device 的 MAE/RMSE/MAPE/(R^2)。

## 5.2 情况 A：gNB 变量显著提升预测

现实部署场景成立，但论文必须把模型放到一个动作闭环中：

1. gNB/O-RAN 估计每 UE radio-energy cost；
2. scheduler 在 QoS、公平性、cell capacity 和 UE energy 之间优化；
3. 通过 PRB/MCS/target-SNR/DRX/BWP/MIMO layer 等可控项执行；
4. 实测至少一个终端结果：总能量、任务完成能量、热节流时间或 battery lifetime；
5. 同时报告网络代价：频谱占用、其他 UE 吞吐、BLER、时延、公平性。

适合的产品化位置是 private 5G/O-RAN，而不是普通 Android App。

## 5.3 情况 B：gNB 变量提升不明显

这并不是失败。应把 gNB 参数重新定位为：

- **causal explanation**：解释何时 RSRP 与功耗关系失效；
- **ground truth/mediator**：确认 UE-only 信号是否通过 Tx power/HARQ/airtime 作用；
- **privileged information/oracle**：给出可部署模型的性能上界；
- **mechanism validation**：验证 AC/RC 差异是否来自 rank、MCS、HARQ 或功控饱和；
- **data-quality audit**：识别 Android API 延迟、量化和 unavailable 值造成的误差。

论文应报告增量价值而非只报各模型绝对精度：

- ΔMAE、ΔRMSE、ΔMAPE、Δ(R^2)；
- bootstrap 95% CI；
- permutation importance/SHAP 但避免把相关解释成因果；
- likelihood-ratio 或 nested-model test；
- 在不同 split 下增益是否稳定；
- 每类 feature 的获取成本和部署权限。

---

# 6. gNB 数据作为训练期 privileged information

## 6.1 已有范式

Vapnik 与 Izmailov 的 LUPI 把训练期额外可见、推理期不可见的信息视为“intelligent teacher”，可用于 similarity control 或 teacher-student knowledge transfer。[JMLR 2015](https://www.jmlr.org/papers/v16/vapnik15b.html)

因此你们的逻辑在机器学习方法上有成熟先例：

\[
F(X_{UE},X_{gNB}) \rightarrow G(X_{UE})
\]

可实现为：

1. **oracle gap**：分别训练 UE-only 与 UE+gNB，量化缺失信息上限；
2. **teacher–student distillation**：teacher 输入 UE+gNB，student 只输入 UE；
3. **privileged-feature prediction**：先由 UE 特征预测 Tx regime/PH/HARQ，再预测功耗；
4. **multitask learning**：主任务为功耗，辅助任务为 Tx regime、BLER/HARQ 或 PH；部署时丢弃辅助头；
5. **causal mediation**：gNB 变量不进入最终预测，而用于估计路径和直接/间接效应。

## 6.2 通信/网络领域是否已有完全相同先例？

本轮找到无线 domain generalization、网络安全 privileged information、以及大量跨模态 teacher–student 工作，但**未找到**明确以“训练时 gNB telemetry、部署时普通 Android telemetry”预测 COTS 手机电功耗的论文。

这应写成“方法范式成熟、目标应用未充分验证”，而不是“新提出 LUPI”。

## 6.3 重要反证

Provodin 等人在 2024 年对 LUPI 的理论和实证基础提出强烈质疑：许多表面增益可能来自数据异常或模型归纳偏置，SOTA LUPI 方法不一定能有效转移 privileged information。[Rethinking Knowledge Transfer in LUPI](https://arxiv.org/abs/2408.14319)

因此论文不能只展示“distillation 比普通 student 好一点”。至少需要：

- 同容量 student；
- 同训练预算；
- hard-label、soft-label、auxiliary-task 和 feature-imputation 基线；
- seed/bootstrap 稳定性；
- OOD 环境/设备测试；
- 检验 student 是否真的学到机制，而非 teacher-induced regularization。

---

# 7. 相关性与因果干预

## 7.1 推荐的因果图

\[
S_{incident},orientation,environment
\rightarrow
RSRP/SINR/CQI/RI
\rightarrow
scheduler/MCS/PRB/HARQ
\rightarrow
TPC/PH/Tx\ power/airtime
\rightarrow
P_{phone}
\]

同时：

\[
traffic,temperature,SOC,CPU,background
\rightarrow P_{phone}
\]

干预变量为：

\[
do(PUSCH\ target\ SNR=s)
\]

而不是把观测到的 TPC 当作外生变量。

## 7.2 主要 estimand

建议预先定义：

1. 固定 offered UL rate 下 target SNR 对平均功率的总效应；
2. 固定 payload 下 target SNR 对任务总能量 (J) 和 (J/MB) 的总效应；
3. 经 Tx regime/PH/HARQ/airtime 的中介效应；
4. 在 PCMAX/PH 接近零和未饱和区间的异质效应；
5. AC 与 RC 对上述效应的调节作用。

## 7.3 必须避免的因果误读

- RSRP 近似固定不代表 UL path loss 完全固定，尤其 TDD reciprocity、UE antenna/orientation、RC 多径和手机内部天线选择会改变；
- target SNR 的变化会改变 scheduler/airtime，不能把总效应只归于 PA；
- PH 是结果/中介，不是纯外生输入；
- 若 goodput 变化，功率和能量/bit 可能方向相反；
- target SNR 顺序应随机化或使用 Latin square，避免温升/SOC 漂移与 treatment 共线。

## 7.4 检索边界的安全表述

强证据支持：

- 闭环功控会改变 UE radiated power、负 PH 和调度行为（NIST；OAI）；
- UE electrical power 对 RF Tx power 呈设备相关、分段非线性（Dusza；Schippers）；
- PRB、MCS 和功控项在规范公式上共同影响 PUSCH power。

本轮未发现：

- 真实 5G SA 手机上以 gNB target SNR 为处理变量、以整机电功耗为结果的直接实验。

投稿前待验证：

- 对 NIST TN 2147、CoPoMo、Schippers 2024 和最新 OAI/private-5G 论文做 Scopus/WoS citing-paper 链；
- 检查 3GPP RAN1/RAN2 contributions 和厂商专利是否有未公开学术验证。

---

# 8. AC 暗室与 RC 混响室

## 8.1 AC 文献的主流端点

主流研究控制或测量：

- conducted path loss/chamber attenuation；
- TRP/TIS/TRS、EIRP、SAR/IPD；
- OTA throughput/sensitivity；
- antenna pattern、orientation、hand/head phantom；
- RSRP 或 test-system reference power。

NIST 的 LTE factor-screening/closed-loop PC 工作具有校准 chamber 和真实 UE，但端点是 emissions。ISED、ITU、3GPP OTA 标准对 incident power density/场强有严格定义，但通常用于合规或 OTA，不与手机电池功耗联合。[ISED IPD measurement procedure](https://ised-isde.canada.ca/site/spectrum-management-telecommunications/en/devices-and-equipment/radio-equipment-standards/radio-standards-specifications-rss/rss-102ipdmeas-measurement-procedure-assessing-incident-power-density-ipd-compliance-accordance-rss)

### 是否已有“校准 DUT 入射场 + 手机自身 RSRP + 手机功耗”？

本轮多组 AC/OTA/incident-field/energy 组合检索未找到清晰的同行评审直接实例。最接近的工作通常只有其中两项：

- 校准物理场 + UE emissions/OTA performance；
- 手机 RSRP + 手机功耗；
- 受控 test-instrument reference power + 模块/手机功耗。

因此该组合可能是**实验方法创新**，但应避免把 \(S_{\rm incident}\) 与 RSRP 当作可互换标量。前者是 DUT 位置的物理场量，后者还包含手机天线、波束、接收链、参考信号结构和报告滤波。

## 8.2 RC 文献的主流端点

### OTA/天线测试

RC 已成熟用于：

- TRP/TIS/TRS；
- MIMO OTA/capacity/throughput；
- sensitivity、carrier aggregation；
- rich-isotropic multipath/Rayleigh-like fading；
- live LTE/5G BS 功能验证。

例如，真实 LTE BS + USB dongle 的 RC 实验研究多径和 FTP 性能，但不测 UE 电功耗。[Barazzetta et al.](https://doi.org/10.1109/ISEMC.2014.6899096)

### 能耗研究

Jörke 2025 在 HIL channel emulator 的 multipath/fading 条件下测 NR/RedCap 模块功耗，是重要邻近工作；Danger 2025 研究 LOS/NLOS/BLOS 与 orientation 对 mmWave 模块能效的影响。但本轮未找到：

\[
same\ mean\ RSRP/incident\ field,
\quad AC/LOS\ vs\ RC/rich\ multipath
\quad \rightarrow\ COTS\ smartphone\ energy
\]

并同步观察 HARQ、MCS、CQI、RI、重传与 Tx behavior 的工作。

## 8.3 你们的 RC 设计应补充的控制

- RC stirrer mode：step-stirred 还是 continuous；
- 每个 stirrer realization 的驻留时间与 UE averaging/filter time；
- chamber loading/Q-factor、PDP、RMS delay spread、K-factor；
- 平均与分位数 SS-RSRP/SS-SINR，而非单一均值；
- orientation/polarization/MIMO rank 的分布；
- AC 与 RC 的 offered load、payload、bandwidth、TDD pattern、scheduler config 完全相同；
- 把环境设为 held-out group，避免同一 realization 泄漏到训练和测试。

---

# 9. Environment generalization

## 9.1 现有模型通常如何验证？

现有实测手机/模块功耗模型多数采用：

- 同一实验或同一商用网络采样后随机 train/test；
- 按设备/运营商/band/SA 状态分别建模；
- 受控 sweep 内插；
- 少量独立 application trace 验证。

Narayanan 2021 的模型是 per-device/per-carrier/per-SA-band 配置；Schippers 2024 的 Tx-power predictor 使用十折/20% 测试但仍来自同类数据；Jopanya 2024 比较多个测试 scheme，但没有 AC→RC 传播域外推。它们不能回答“新传播环境是否泛化”。

无线 ML 社区已经明确指出 i.i.d. 假设在真实无线 domain shift 下经常失效，并把 unseen environment generalization 视为开放问题。[Akrout et al., Domain Generalization in Wireless Communications](https://arxiv.org/abs/2303.08106)

## 9.2 推荐验证矩阵

| Split | 回答的问题 | 最低要求 |
|---|---|---|
| within-AC grouped test | 同环境内可重复性 | 按 run/day 分组，不按样本随机 |
| leave-orientation-out | 未见姿态 | 整个角度/角度区间 held out |
| AC→RC | 传播域外推 | RC 不参与 feature scaling/model selection |
| RC→AC | 反向域外推 | 检查非对称性 |
| pooled AC+RC→held-out condition | 多域学习 | held-out power/traffic/orientation block |
| leave-device-out | 硬件迁移 | 至少 3 款、最好跨 chipset/OEM |
| leave-day/thermal-session-out | 仪器/热漂移 | session 为 group |

指标除 MAE/RMSE/MAPE/(R^2) 外，还应报告：

- calibration curve 与 error vs condition；
- power 与 energy/bit 两个目标；
- target-SNR intervention effect 的预测误差；
- OOD degradation ratio；
- 预测区间覆盖率；
- gNB-assisted 相对 UE-only 的 Δmetric 及置信区间。

---

# 10. COTS Android 可获得变量

## 10.1 普通 Android App 可获得

| 变量 | 公共 API 依据 | 限制 |
|---|---|---|
| SS-RSRP/SS-RSRQ/SS-SINR | `CellSignalStrengthNr`, API 29 | 未报告时为 `CellInfo.UNAVAILABLE` |
| serving/neighbor cell info、cell identity、NR 类型 | `TelephonyManager.requestCellInfoUpdate` / `CellInfoNr` | 需 `ACCESS_FINE_LOCATION`；更新限频且不保证即时 |
| app-level throughput/traffic workload | socket/TrafficStats/应用自身计数 | 不是 MAC goodput；时间窗需与 gNB 对齐 |
| battery current now/average、charge counter、capacity | `BatteryManager`, API 21 | fuel-gauge 实现/滤波/符号因设备而异；属性可能不支持 |
| voltage、battery temperature、SOC | `ACTION_BATTERY_CHANGED`/BatteryManager | 更新粒度和精度设备相关 |
| thermal status/headroom | `PowerManager`, API 29/30 | headroom 不支持时 NaN；不等于具体 modem 温度 |
| timing advance | `CellSignalStrengthNr.getTimingAdvanceMicros`, API 34 | 仅 RRC active 且设备上报时可用 |

官方文档明确给出 SS-RSRP/RSRQ/SINR 的 NR API，并说明 `UNAVAILABLE`；[CellSignalStrengthNr](https://developer.android.com/reference/android/telephony/CellSignalStrengthNr)。`BatteryManager` 给出瞬时/平均电流和 charge counter，但平均窗口由 fuel-gauge hardware/configuration 决定；[BatteryManager](https://developer.android.com/reference/android/os/BatteryManager)。

## 10.2 部分设备/API 可获得

| 变量 | 状态 | 依据/限制 |
|---|---|---|
| CSI-RSRP/RSRQ/SINR | API 29 公共方法 | 网络/设备未上报则 `UNAVAILABLE` |
| CSI-CQI report/table | API 31 公共方法 | CQI 不可用时返回空列表；并非所有机型稳定提供 |
| thermal headroom | API 30 | 不支持返回 NaN；高频调用无益 |
| charge/energy/current accuracy | 公共属性存在 | 是否真实、刷新率、校准和量化均依 OEM |

`getCsiCqiReport()` 确实是公共 API，而非一定需要 root；但“方法存在”不等于“每台 COTS 手机都会提供有效 CQI”。[Android CQI API](https://developer.android.com/reference/android/telephony/CellSignalStrengthNr#getCsiCqiReport())

## 10.3 普通 App 基本无法获得

- UE RF Tx power；
- TPC command/history；
- raw/normalized PH/PHR；
- exact per-TTI/slot PRB allocation；
- exact UL/DL MCS；
- HARQ rounds/retransmission process；
- gNB PUSCH SNR/RSSI、BLER/DTX；
- scheduler state/queue/grant decisions；
- UL RI/TPMI 等详细 PHY/MAC 状态。

Android 公共 telephony API 没有上述接口。支持这一判断的实证证据是：Xu 2020 必须通过 USB 连接 XCAL-Mobile diagnostic interface 获取 CQI/MCS 等；Narayanan 2021 的部分工具需要 root；Schippers 2024 明确指出许多商业 Android 手机不在应用层暴露 Tx power。

因此论文应把“public Android deployable”写成一份**实测 availability matrix**：手机型号 × Android 版本 × API × 有效值比例 × 更新间隔，而不是根据 API 列表假定可用。

---

# 11. OAI/Open5GS/srsRAN/private 5G 的相似工作

## 11.1 最接近组合

### Staal 2025

OAI 5G SA + 商用 Quectel 模块 + Keysight 功率测量 + bandwidth/throughput/MCS，是最接近“可编程 gNB + COTS UE + power”的公开工作；差异是 conducted RF、模块而非手机、无 AC/RC、无 gNB-feature prediction、无因果 target-SNR 实验。

### X5G/O-RAN testbeds

X5G 等开放 private 5G/O-RAN testbed 可连接多台 COTS smartphones并进行 iperf/视频/性能实验，但公开论文重点是平台与吞吐，不是手机电功耗。[X5G](https://arxiv.org/abs/2406.15935)

### Schippers 2024

商业模块 + 5G SA/NSA + test instrument + power meter，实验控制很强，但不是开放 gNB/真实手机/chamber。

### Xu/Narayanan

真实手机 + 能耗 + 跨层观测很强，但网络不可控、主要是 NSA/商用网络，不能做 scheduler/power-control intervention。

### NIST

受控 chamber + 商用 UE + network power control 很强，但 LTE 且端点为 emissions。

## 11.2 综合结论

本轮未发现与“**可编程 OAI 5G SA gNB + COTS smartphone + OTA AC/RC + phone electrical power + synchronized gNB telemetry + target-SNR intervention**”高度同构的同行评审实验。这个判断来自多个“各占一部分”的最接近工作，而不是单纯关键词未命中。

---

# 12. 三个论文定位评分

评分 1–5；“reviewer attack surface”分数越高表示越容易被攻击。

| 维度 | Position A: Cross-layer UE energy prediction | Position B: Causal characterization + UE-only estimator | Position C: RAN-side estimation for scheduling |
|---|---:|---:|---:|
| Novelty | 2.5 | **4.5** | 3.5 |
| Significance | 3.0 | **4.5** | 4.0 |
| Deployability | 2.0 | **4.0** | 3.5（private/O-RAN） |
| Experimental strength | 4.5 | **5.0** | 4.5 |
| Reviewer attack surface | **4.5** | 3.0 | 4.0 |
| High-level venue suitability | 2.5 | **4.5** | 4.0（需闭环） |

## 12.1 Position A：Cross-layer UE energy prediction

**优点**：数据丰富、实验可重复、可得到很高预测精度；可能是首批真实 5G SA phone + gNB telemetry 模型。

**主要攻击**：

- LTE CoPoMo、5G 模块和 5G phone UE-only 模型已覆盖大部分思想；
- privileged features 普通 App 不可获得；
- 精度提升可能来自时间/场景泄漏；
- gNB 增益若小，significance 很弱；
- 只预测、不优化，系统贡献不足。

**判断**：适合作为论文的一部分或数据集/measurement contribution，不宜单独作为顶级主线。

## 12.2 Position B：Cross-layer causal characterization + UE-only deployable estimator

**优点**：把最强实验能力转化为机制问题；无论 gNB 增益大或小都有可发表结论；解决 deployability 质疑；AC/RC 和 target-SNR intervention 形成独特的 experimental story。

**主要攻击**：

- target SNR 改变多个中介，因果识别必须严谨；
- LUPI/distillation 不保证成功；
- UE-only model 可能 device-specific；
- Android battery current 的 ground truth 质量。

**建议 venue**：若系统实现和实验规模足够，优先考虑 MobiSys/IMC/INFOCOM/TMC；若无线机制更强，TWC/TCOM；若主要是 measurement + model，TMC/IMC/CCNC/VTC 分层选择。

**判断**：最推荐。

## 12.3 Position C：RAN-side UE energy estimation for energy-aware scheduling

**优点**：gNB 原生可见所需参数；与 UE Assistance、3GPP energy information、private 5G/O-RAN 趋势一致；系统价值比纯预测更明确。

**主要攻击**：

- 若不实现 scheduler closed loop，只是部署愿景；
- 真实 battery/SOC/thermal/foreground load 网络不可见；
- 不同手机的电功耗模型异质；
- 单 UE 节能可能牺牲 cell capacity、其他 UE、公平性；
- 3GPP 新工作可能使“per-UE energy information”不再新，必须强调你们估计的是**UE electrical/radio energy**而非网络节点能耗按数据量分摊。

**判断**：若能增加 2–4 UE、实现 energy-aware scheduler 并比较 cell-level trade-off，可成为 Position B 的强扩展或独立系统论文；否则保持为未来工作。

---

# 13. 推荐论文问题、实验与分析框架

## 13.1 建议主标题方向

> **Beyond Signal Strength: Causal and Cross-Environment Characterization of 5G SA Smartphone Energy with Programmable RAN Telemetry**

或更部署导向：

> **From RAN-Privileged Telemetry to Deployable Smartphone Energy Estimation in Controlled 5G SA Environments**

## 13.2 核心研究问题

1. 在固定下行覆盖和业务下，PUSCH target-SNR intervention 对手机功率与任务能量的因果效应是多少？
2. 该效应通过 TPC、PH/PCMAX、MCS/PRB、HARQ/airtime 的哪些路径形成？
3. 同一平均 RSRP/入射场下，AC 与 RC 是否产生不同能耗？哪些 gNB state 解释差异？
4. gNB telemetry 相比 UE-only 特征的增量预测价值是多少？该价值在 OOD 环境和设备上是否仍存在？
5. 训练期 gNB privileged information 能否改善部署期 UE-only estimator？

## 13.3 最低可接受实验设计

- 至少 3 款手机，最好跨 OEM/芯片平台；
- 每个条件多 run、多 day，treatment 顺序随机化；
- AC 多 orientation；RC 多 stirrer realization；
- UL 固定速率、固定 payload、饱和、burst；DL 至少选一个对照；
- target SNR 多级，不只 high/low；覆盖 PH 正、接近零、PCMAX 饱和区；
- 同时记录 phone current/voltage/charge/temp/SOC/thermal、UE radio API、gNB slot/interval 聚合；
- 统一时钟并估计 logging latency；
- 若条件允许，用外部功率仪对 Android current 做一部分 calibration；
- 屏幕、CPU governor、后台服务、充电状态、SOC、温度窗口固定；
- 预注册/预定义主要 estimand 和 split，减少 post-hoc storytelling。

## 13.4 模型基线

\[
M_0=\text{mean/device-state baseline}
\]

\[
M_1=f(RSRP)
\]

\[
M_2=f(SS\text{-}RSRP,SS\text{-}RSRQ,SS\text{-}SINR,traffic,throughput,temp,SOC)
\]

\[
M_3=f(M_2,TPC,PH,PCMAX,PUSCH\ SNR/RSSI,MCS,PRB,HARQ,CQI,RI,BLER)
\]

\[
M_4=student(M_2)\leftarrow teacher(M_3)
\]

需要同时有：线性/分段物理模型、tree boosting、一个时序模型；不建议以大量算法横向比武替代科学问题。

---

# 14. 最终研究空白

## 14.1 可直接用于 proposal/paper 的审慎表述

> Existing studies have separately investigated signal strength, UE transmit power, resource allocation, standardized NR activity states, and smartphone or modem energy consumption. LTE-era models already include transmit power, PRBs, MCS, traffic, and context, while recent 5G studies model commercial-phone power from throughput and signal strength or profile commercial modules under controlled transmit-power and bandwidth settings. Standards and industry work further establish practical network-assisted UE power-saving and energy-information use cases. However, within the literature reviewed here, limited evidence was found for controlled over-the-air 5G SA experiments that jointly observe calibrated incident radio conditions, gNB-side power-control and scheduling states, and whole-device electrical power of COTS smartphones. In particular, the search did not identify a study that intervenes on the gNB uplink target SNR while holding downlink coverage approximately constant, compares UE-only and gNB-assisted estimators on the same devices, and evaluates transfer between deterministic and rich-multipath environments. These gaps should be treated as evidence-limited opportunities rather than claims of absolute priority.

## 14.2 证据强弱标注

### 有强文献证据

- 信号强度、吞吐、RRC/DRX、UE Tx power 与手机/模块功耗有关；
- LTE 模型已使用 PRB/MCS/资源分配；
- 5G COTS phone 已有 UE-only prediction；
- 5G commercial module 已有 Tx-power/BW/PRB/MCS profiling；
- 网络辅助 UE 节能有标准和商用 UE 实证；
- AC/RC 已成熟用于 calibrated OTA/emissions/performance；
- 普通 Android API 与 diagnostic/root/gNB telemetry 的可见性边界明确。

### 本轮检索未找到直接证据

- 真实 5G SA phone 上 target-SNR intervention → battery power；
- PH/TPC/HARQ/scheduler 联合预测真实整机功耗；
- DUT 校准入射场 + phone RSRP + phone energy 三者同步；
- matched-RSRP AC vs RC 的 COTS phone energy comparison；
- AC-train → RC-test 的手机功耗模型；
- gNB-privileged train → Android-only deployment 的手机功耗 LUPI 实例。

### 投稿前进一步验证

- IEEE Xplore/Scopus/WoS 中对 CoPoMo、NIST TN 2147、Xu 2020、Narayanan 2021、Schippers 2024 的 citing-paper 全链；
- 2025–2026 年 MILCOM、CCNC、PIMRC、INFOCOM、TMC/TWC 早访问论文；
- 3GPP RAN1/RAN2/O-RAN WG 中 UE energy estimation 的最新 contribution；
- Qualcomm/Samsung/Ericsson/Nokia 专利与公开测试报告中是否已有 target-SNR/battery 验证。

---

# 15. 结论

最有说服力的论文不是证明“gNB 变量能把 RMSE 再降一点”，而是利用可编程 OAI、AC、RC 和真实手机建立一个审稿人难以复制的证据链：

1. **干预**网络功控而不只观测信号；
2. **解释**手机能耗如何由功控、调度、重传和传播环境共同形成；
3. **量化**gNB privileged information 的真实增量；
4. **验证**跨环境和跨设备失效，而不是隐藏失效；
5. **交付**普通 Android 可用的 student，或在增益足够大时交付 RAN-side closed loop。

这一路线既主动容纳“gNB 变量没有帮助”的否定结果，也能在变量确有帮助时自然升级到 private 5G/O-RAN energy-aware scheduling；因此比单纯的 cross-layer prediction 更稳健、更有实际意义，也更符合高水平系统/网络论文对机制、泛化和部署闭环的要求。
