import { createContext, useContext, useLayoutEffect, useMemo, useState, type ReactNode } from 'react';

export type Language = 'en' | 'zh';

const LanguageContext = createContext<{ language: Language; setLanguage: (value: Language) => void }>({
  language: 'en', setLanguage: () => undefined,
});

const zh: Record<string, string> = {
  'Language': '语言', 'English': '英文', 'Chinese': '中文', 'live': '在线',
  '5G Energy Platform': '5G 能耗实验平台', 'research instrument': '科研仪器', 'OAI lab': 'OAI 实验室',
  'platform /': '平台 /',
  'Current experiment and run context': '当前实验与 Run 上下文',
  'Dashboard': '仪表板', 'Experiments': '实验', 'Data Import': '数据导入', 'Advanced': '高级',
  'Result Workspace': '结果工作区', 'Experiment': '实验', 'Environment': '环境',
  'Configuration': '配置', 'Workflow': '工作流', 'Run': 'Run', 'Status': '状态', 'Quality': '质量',
  'Advanced / Research Tools': '高级 / 科研工具',
  'Hardware diagnostics and administrative settings outside the normal Run workflow.': '常规 Run 工作流之外的硬件诊断与管理设置。',
  'RC Hardware': 'RC 硬件', 'System Settings': '系统设置', 'Open diagnostics': '打开诊断',
  'Open settings': '打开设置', 'Connection, manual jog, DLL, helper and simulation diagnostics.': '连接、手动点动、DLL、辅助程序与仿真诊断。',
  'OAI connectivity and administrative settings.': 'OAI 连接与管理设置。',
  'Page render failed': '页面渲染失败', 'Reload the page or return to Dashboard.': '请重新加载页面或返回仪表板。',

  'Run Control': 'Run 控制', 'Loading Run Control…': '正在加载 Run 控制…', 'Refresh': '刷新', 'Phone': '手机', 'Storage': '存储',
  'gNB Current State': 'gNB 当前状态', 'UE Sync': 'UE 同步', 'CURRENTLY SYNCED': '当前已同步',
  'WAITING': '等待中', 'RUNNING': '运行中', 'STOPPED': '已停止', 'CONNECTED': '已连接', 'DISCONNECTED': '未连接',
  'Run Context': 'Run 上下文', 'Selected for next Run': '下次 Run 使用',
  'A fresh Run ID and standard timing values are generated automatically on every Start.': '每次开始时自动生成新的 Run ID 与标准时序值。',
  'Select a Configuration, check external dependencies, then Start. Every Start applies it and force-restarts gNB.': '选择配置并检查外部依赖后开始；每次开始都会应用配置并强制重启 gNB。',
  'Review the complete RC Workflow, check external dependencies, then Start.': '检查完整 RC 工作流与外部依赖后开始。',
  'Start always reapplies the Workflow and restarts gNB': '开始时始终重新应用工作流并重启 gNB',
  'Established after Start and recorded as the Run time anchor': '开始后建立同步，并记录为 Run 时间锚点',
  'Device unavailable': '设备不可用',
  'Workflow State': '工作流状态', 'RC Workflow State': 'RC 工作流状态', 'RC Workflow': 'RC 工作流',
  'Configuration State': '配置状态', 'Default': '默认', 'Selected': '已选择', 'Currently Applied': '当前已应用',
  'Active Run Snapshot': '活动 Run 快照', 'Apply without starting': '仅应用，不开始',
  'Apply Workflow without starting': '仅应用工作流，不开始', 'Applying & verifying…': '正在应用并验证…',
  'Applied is informational. Start always reapplies Selected and force-restarts gNB.': '已应用状态仅供参考；开始时始终重新应用所选配置并强制重启 gNB。',
  'The complete Workflow is reapplied and gNB is restarted on every Start.': '每次开始都会重新应用完整工作流并重启 gNB。',
  'SELECTED FOR NEXT RUN': '下次 RUN 使用', 'CURRENT MATCH': '当前一致', 'WILL APPLY ON START': '开始时应用',
  'Unknown': '未知', 'Requested vs Applied': '请求值与应用值', 'Requested': '请求值', 'Applied': '应用值', 'DIFFERENT': '不一致',
  'Preflight Checklist': '预检清单', 'Ready to Start · gNB restart and UE synchronization happen after Start': '可以开始；gNB 重启与 UE 同步会在开始后进行',
  'issue(s) need attention:': '个问题需要处理：',
  'Experiment selected': '已选择实验', 'Workflow complete': '工作流完整', 'Configuration complete': '配置完整',
  'OAI control endpoint reachable': 'OAI 控制端点可访问', 'Unplug USB before Start': '开始前请拔出 USB',
  'Storage available': '存储可用', 'RC Chamber': 'RC 混响室', 'Stirrer hardware available': '搅拌器硬件可用',
  'Run Summary': 'Run 摘要', 'Review the selected conditions before execution. Run ID is assigned only when Start is accepted.': '执行前检查实验条件；仅在开始请求被接受后分配 Run ID。',
  'Run cannot start': '无法开始 Run', 'Execution Mode': '执行模式', 'Generated automatically on Start': '开始时自动生成',
  'Radio': '无线参数', 'Start Run': '开始 Run', 'Stop Run': '停止 Run', 'Restarting gNB…': '正在重启 gNB…', 'Stopping…': '正在停止…',
  'REAL_HARDWARE': '真实硬件',
  'Active Run Monitor': '活动 Run 监控', 'Phase, gNB and measurement state are platform-authoritative.': 'Phase、gNB 与测量状态以平台记录为准。',
  'End task now': '立即结束任务', 'Current Phase': '当前 Phase', 'Standard Run': '标准 Run',
  'LOADED Configuration': 'LOADED 配置', 'Sample / Angle': '样本 / 角度', 'Sample Phase': '样本 Phase',
  'RSSP / Target': 'RSSP / 目标值', 'Latest activity': '最新活动', 'Preparing': '准备中',
  'Live Throughput / RSSP / SNR': '实时吞吐率 / RSSP / SNR',
  'Run-scoped 1 Hz gNB telemetry; hover to read the exact timestamp and values.': 'Run 范围内的 1 Hz gNB 遥测；悬停可查看精确时间戳与数值。',
  'Waiting for Run telemetry…': '等待 Run 遥测数据…', 'UL throughput': 'UL 吞吐率',
  'Power Calibration Record': '功率校准记录',
  'Each point is one RSSP observation. Hover shows Sample, TX Gain, Target SNR and RSSP.': '每个点代表一次 RSSP 观测；悬停显示样本、TX Gain、Target SNR 与 RSSP。',
  'Calibration points will appear when the first Sample reaches power calibration.': '首个样本进入功率校准后显示校准点。',

  'Overview': '概览', 'Configurations': '配置', 'History': '历史', 'Manage Experiment': '管理实验',
  'New Experiment': '新建实验', '+ New Experiment': '+ 新建实验', 'Create Experiment': '创建实验', 'Create & Configure': '创建并配置',
  'Create Workflow': '创建工作流',
  'Search ID, operator or purpose': '搜索 ID、操作员或目的', 'All environments': '所有环境',
  'Search experiments': '搜索实验', 'Environment filter': '环境筛选', 'Sort experiments': '实验排序',
  'Newest created': '最新创建', 'Oldest created': '最早创建', 'Last activity': '最近活动',
  'No purpose recorded.': '未记录实验目的。', 'Operator': '操作员', 'Runs / History': 'Run / 历史',
  'Last result': '最近结果', 'More actions': '更多操作', 'Created': '创建于',
  'Configure here. Review here. Run from Dashboard.': '在此配置与复核，并在仪表板中运行。',
  'Export experiment': '导出实验', 'Delete experiment': '删除实验', 'Cancel': '取消',
  'Create the Experiment first, then manage its Configurations.': '先创建实验，再管理其配置。',
  'Create the Experiment, then edit its single complete Workflow.': '创建实验后，编辑其唯一的完整工作流。',
  'A Default Configuration will be created automatically and shown explicitly after creation.': '创建后将自动生成并明确显示默认配置。',
  'One complete RC Workflow will be created automatically. RC has no Default or alternate Configuration.': '将自动创建一个完整的 RC 工作流；RC 不存在默认或备选配置。',
  'Experiment ID': '实验 ID', 'Created time': '创建时间', 'Purpose': '目的', 'Flow': '流程', 'Notes': '备注',
  '← Experiments': '← 实验', 'Experiment management · execution controls are on Dashboard': '实验管理 · 执行控制位于仪表板',
  'Experiment metadata. ID, environment and creation time are read only.': '实验元数据；ID、环境和创建时间为只读。',
  'Save changes': '保存更改', 'Saving…': '正在保存…', 'Default Configuration': '默认配置',
  'Saved Configurations': '已保存配置', 'Add Configuration': '添加配置', '+ Add Configuration': '+ 添加配置', 'Edit Configuration': '编辑配置',
  'Duplicate': '复制', 'Set as default': '设为默认', 'Archive': '归档', 'Retry': '重试',
  'Configuration name': '配置名称', 'Save Configuration': '保存配置', 'Edit RC Workflow': '编辑 RC 工作流',
  'Save Workflow': '保存工作流', 'Unsaved changes are protected when you leave this editor.': '离开编辑器时会保护未保存的更改。',
  'AC Run Template': 'AC Run 模板', 'Enable Template for this Experiment': '为此实验启用模板',
  'Optional platform-owned Phase sequence. Completion automatically sends End Experiment.': '可选的平台 Phase 序列；完成后自动发送结束实验信号。',
  'Duration (s)': '时长（秒）', 'Radio remains idle': '无线链路保持空载', 'Remove': '移除',
  '+ IDLE Phase': '+ IDLE Phase', '+ LOADED Phase': '+ LOADED Phase',
  'Save Template': '保存模板', 'RF': '射频', 'Frequency': '频率', 'Bandwidth': '带宽',
  'PUSCH': 'PUSCH', 'Mode': '模式', 'Auto': '自动', 'Manual': '手动', 'Modulation': '调制方式',
  'dB · stored internally as ×10': 'dB · 内部按 ×10 存储', 'Mbps · values ≥100 request saturation': 'Mbps · 值 ≥100 时请求饱和负载',
  'UL Scheduler': 'UL 调度器', 'Traffic': '流量', 'UL Traffic': 'UL 流量',
  'RC Chamber Workflow': 'RC 混响室工作流', 'Time Sync': '对时', 'Power Calibration': '功率校准',
  'Noise Capture': '噪声采集', 'Loaded Capture': '带载采集', 'Stirrer Rotation': '旋转搅拌器',
  'Clock exchanges': '对时交换次数', 'Maximum RTT': '最大 RTT', 'Tolerance': '容差',
  'Max iterations': '最大迭代次数', 'Servo settle': '伺服稳定时间', 'Adjust Target SNR': '调整 Target SNR',
  'Adjust TX Gain': '调整 TX Gain', 'Enabled': '启用', 'Disabled': '禁用', 'Noise frames': '噪声帧数',
  'Noise margin': '噪声裕量', 'Peak prominence': '峰值显著度', 'Delay window start': '时延窗口起点',
  'Delay window end': '时延窗口终点', 'Measurement dwell': '测量驻留时间', 'Mechanical settle': '机械稳定时间',
  'Step angle': '步进角度', 'Samples': '样本数', 'Speed': '速度', 'Real hardware': '真实硬件', 'Simulation': '仿真',
  'One RC Experiment has one complete Workflow; there is no Default or alternate Configuration.': '每个 RC 实验只有一个完整工作流，不存在默认或备选配置。',
  'Edit Workflow': '编辑工作流', 'RC Workflow is unavailable.': 'RC 工作流不可用。',
  'Used for the next Run unless Dashboard explicitly selects another Configuration.': '用于下一个 Run，除非在仪表板中明确选择其他配置。',
  'Cards are read-only until an explicit action is selected.': '配置卡默认只读，仅在明确选择操作后可编辑。',
  'No non-default Configurations.': '没有非默认配置。', 'View full configuration': '查看完整配置',
  'Edit': '编辑', 'READY': '已就绪', 'DEFAULT FOR NEXT RUN': '下次 RUN 的默认配置',
  'Scheduler': '调度器',
  'Run History': 'Run 历史', 'Batch read all results': '批量读取全部结果', 'Time': '时间', 'Result': '结果', 'Actions': '操作',
  'Newest first. Each Run stores its execution-time Workflow snapshot.': '最新记录优先；每个 Run 都保存执行时的工作流快照。',
  'Newest first. Each Run uses its execution-time Configuration snapshot.': '最新记录优先；每个 Run 都使用执行时的配置快照。',
  'View result': '查看结果', 'Previous': '上一页', 'Next': '下一页', 'Result Detail': '结果详情',
  'Page': '第', 'of': '/', 'ERROR': '错误', 'Close': '关闭', 'Condition ID': '条件 ID', 'State': '状态',
  'Started': '开始时间', 'Completed': '完成时间', 'Requested Configuration': '请求配置',
  'Applied / Verified Configuration': '已应用 / 已验证配置', 'Open Result Workspace': '打开结果工作区',
  'Phone records': '手机记录', 'gNB records': 'gNB 记录', 'CIR records': 'CIR 记录', 'Clips': 'Clip',
  'Run actions': 'Run 操作',
  'Danger Zone': '危险操作区', 'Export everything': '导出全部', 'Delete Experiment': '删除实验',
  'This removes its Workflow or Configurations, Runs and derived files.': '这将删除其工作流或配置、Run 与派生文件。',

  'USB Connection': 'USB 连接', 'Matched Experiments': '已匹配实验', 'Phone Runs': '手机 Run',
  'Runs to Import': '待导入 Run', 'Phone Data Inventory (USB)': '手机数据清单（USB）',
  'Refresh Inventory': '刷新清单', 'Loading…': '正在加载…', 'Loading phone inventory…': '正在读取手机清单…',
  'Import All': '全部导入', 'First Sample (UTC)': '首个样本（UTC）', 'Last Sample (UTC)': '末个样本（UTC）',
  'Phone Samples': '手机样本', 'Platform Samples': '平台样本', 'Platform Status': '平台状态',
  'Import / Update': '导入 / 更新', 'No phone data': '手机无数据', 'Importing…': '正在导入…',
  'serial': '序列号', 'All Experiments reconciled': '所有实验已对账',
  'Phone has additional samples': '手机端有新增样本', 'Both': '两端均有',
  'Platform-only': '仅平台', 'Phone-only': '仅手机', 'Runs ·': '个 Run ·', 'samples': '个样本',
  'Phone-only, Platform-only and Both are reconciled explicitly. Import replaces the platform copy idempotently by Run ID and never modifies phone source data.': '明确对账仅手机、仅平台与两端均有的数据；导入会按 Run ID 幂等替换平台副本，不会修改手机源数据。',

  'RC Hardware Diagnostics': 'RC 硬件诊断', 'Driver Readiness': '驱动就绪状态',
  'Connection & Manual Jog': '连接与手动点动', 'Controller': '控制器', 'Position': '位置', 'Runtime': '运行环境',
  'Diagnostic mode': '诊断模式', 'Controller COM port': '控制器 COM 口', 'Jog angle (°)': '点动角度（°）',
  'Connect': '连接', 'Disconnect': '断开',
  '← Advanced': '← 高级', 'Motor idle': '电机空闲', 'REAL HARDWARE': '真实硬件',
  'Read-only checks used by Run Control Preflight.': '供 Run 控制预检使用的只读检查。',
  'FOUND': '已找到', 'Helper READY': '辅助程序已就绪', 'USB auto': 'USB 自动',
  'Diagnostic connection only': '仅诊断连接', 'Session': '会话', 'CLOSED': '已关闭', 'OPEN': '已打开',
  'The selected COM port is saved and reused by RC Campaigns.': '所选 COM 口会保存并供 RC Campaign 重用。',
  'Connection checks and manual jog only. Workflow Preflight and Campaign execution remain in Run Control.': '此处仅执行连接检查与手动点动；工作流预检和 Campaign 执行位于 Run 控制。',
  'This page does not save the RC Workflow or start and stop Runs. Execution Mode belongs to the single RC Workflow.': '此页面不保存 RC 工作流，也不启动或停止 Run；执行模式由唯一 RC 工作流设置。',

  'Settings': '设置', 'OAI connectivity and control token': 'OAI 连接与控制令牌',
  'OAI Connectivity Settings': 'OAI 连接设置', 'Control token': '控制令牌', 'Save settings': '保存设置',
  'saving…': '正在保存…', 'clear configured token': '清除已配置令牌', 'Note': '说明',
  'OAI host': 'OAI 主机', 'OAI port': 'OAI 端口', 'leave blank to keep current': '留空以保持当前值',
  'not set': '未设置', 'optional': '可选',
  'The control token is only ever read from environment / local secret config and is never returned in plaintext. This page shows whether a token is configured, and lets you set or clear it — but never displays its value.': '控制令牌仅从环境或本地密钥配置中读取，绝不以明文返回。此页仅显示是否已配置令牌，并允许设置或清除，但不会显示其值。',

  'One Run · one Master Timeline · non-destructive Clip composition': '一个 Run、一条主时间线、非破坏性 Clip 组合',
  'Change Run': '切换 Run', 'Experiment History': '实验历史', '← Experiment History': '← 实验历史', 'Workflow Snapshot': '工作流快照',
  'Configuration Snapshot': '配置快照', 'Not evaluated': '未评估', 'Data Completeness & Timeline Alignment': '数据完整性与时间线对齐',
  'Channel': '信道', 'Resolved Paths': '已解析路径', 'RMS Delay': 'RMS 时延', 'Delay Resolution': '时延分辨率',
  'Noise / Threshold': '噪声 / 阈值', 'Link Reliability': '链路可靠性', 'HARQ Retransmission': 'HARQ 重传',
  'Performance': '性能', 'UL Goodput': 'UL 有效吞吐率', 'DL Goodput': 'DL 有效吞吐率', 'Chamber': '混响室',
  'Stirrer Angle': '搅拌器角度', 'Sample': '样本', 'Window': '窗口', 'RC Sample / Stirrer Track': 'RC 样本 / 搅拌器轨迹',
  'PDP Heatmap': 'PDP 热力图', 'Master Timeline Controls': '主时间线控制', 'Fit Run': '适配 Run',
  'T+0 = sync_before · no hidden time correction': 'T+0 = sync_before · 无隐式时间校正',
  'ALIGNED': '已对齐', 'UNAVAILABLE': '不可用', 'records': '条记录', 'Raw offset': '原始偏移', 'Applied correction': '已应用校正',
  'Drag on the shared ruler to create a timestamp-based Global Selection.': '在共享标尺上拖动，以创建基于时间戳的全局选区。',
  'phone': '手机', 'gnb': 'gNB', 'events': '事件', 'Partial': '部分数据',
  'Zoom to Selection': '缩放至选区', 'Start': '开始', 'End': '结束', 'Duration': '时长', 'Events': '事件',
  'Run transitions and RC Measurement Window markers use the same Master Timeline.': 'Run 状态迁移与 RC 测量窗口标记使用同一条主时间线。',
  'PREPARING': '准备中', 'ARMED': '已就绪',
  'Battery power and RSRP · gaps are preserved': '电池功率与 RSRP；保留数据间断', 'Selected PDP': '所选 PDP',
  'PDP unavailable.': 'PDP 不可用。', 'Path': '路径', 'Delay': '时延', 'Excess Delay': '超额时延',
  'Phase': '相位', 'Above Threshold': '高于阈值', 'Prominence': '显著度', 'Confidence': '置信度',
  'Advanced Channel View': '高级信道视图', 'Processing Metadata': '处理元数据', 'Algorithm': '算法',
  'Link Reliability & Performance': '链路可靠性与性能',
  'BLER is a fraction rendered as percent; HARQ is OAI interval-delta rate': 'BLER 为按百分比显示的比例；HARQ 为 OAI 区间差分速率',
  'Advanced Channel Data': '高级信道数据',
  'AC Result stays focused on Phone, power, radio, link, events, Timeline and Clips. Raw channel data is loaded only on request.': 'AC 结果聚焦手机、功率、无线参数、链路、事件、时间线与 Clip；仅在请求时加载原始信道数据。',
  'Load channel data': '加载信道数据', 'No CIR/PDP request has been made for this AC Result.': '尚未为此 AC 结果请求 CIR/PDP 数据。',
  'Clip Composition': 'Clip 组合', 'One Clip may contain multiple non-contiguous Segments from this Run; Source Time remains immutable.': '一个 Clip 可包含此 Run 中多个不连续的片段；源时间保持不变。',
  'Add Selection to Clip': '将选区添加到 Clip', 'Clip name': 'Clip 名称', 'Select a Master Timeline range, then add it to the draft.': '选择主时间线范围，再添加到草稿。',
  'Save Clip': '保存 Clip', 'Segments': '片段',
  'Capability Boundaries': '能力边界', 'Unavailable': '不可用', 'Raw PDP': '原始 PDP', 'power': '功率',
};

const textState = new WeakMap<Text, { source: string; last: string }>();
const attrState = new WeakMap<Element, Map<string, { source: string; last: string }>>();

function translated(source: string): string {
  const match = source.match(/^(\s*)(.*?)(\s*)$/s);
  if (!match) return source;
  const [, before, value, after] = match;
  let result = zh[value];
  if (!result) {
    result = value
      .replace(/^(\d+) issue\(s\) need attention:?$/, '$1 个问题需要处理：')
      .replace(/^(\d+) external dependency issue\(s\) need attention$/, '$1 个外部依赖问题需要处理')
      .replace(/^(\d+) indexed files$/, '已索引 $1 个文件')
      .replace(/^platform \/(.+)$/, '平台 /$1')
      .replace(/^(v[\d.]+ · )OAI lab$/, '$1OAI 实验室')
      .replace(/^More actions for (.+)$/, '$1 的更多操作')
      .replace(/^serial (.+)$/, '序列号 $1')
      .replace(/^(\d+) Experiments · page (\d+) of (\d+)$/, '$1 个实验 · 第 $2 / $3 页')
      .replace(/^Configurations \((\d+)\)$/, '配置（$1）')
      .replace(/^History \((\d+)\)$/, '历史（$1）')
      .replace(/^Manual · (.+)$/, '手动 · $1')
      .replace(/^(\d+) samples · REAL_HARDWARE$/, '$1 个样本 · 真实硬件')
      .replace(/^Created (.+) · used by (\d+) Run\(s\)$/, '创建于 $1 · 被 $2 个 Run 使用')
      .replace(/^(.+) FOUND$/, '$1 已找到')
      .replace(/^(.+) READY$/, '$1 已就绪')
      .replace(/^Page (\d+) of (\d+)$/, '第 $1 / $2 页')
      .replace(/^(\d+) Runs · (\d+) samples$/, '$1 个 Run · $2 个样本')
      .replace(/^(\d+) platform-only Experiments$/, '$1 个仅平台可见的实验')
      .replace(/^(\d+) phone-only Experiments$/, '$1 个仅手机可见的实验')
      .replace(/^Phase (\d+)$/, 'Phase $1')
      .replace(/^Created (.+)$/, '创建于 $1');
  }
  return before + result + after;
}

function localizeText(node: Text, language: Language) {
  const current = node.data;
  let state = textState.get(node);
  if (!state || current !== state.last) state = { source: current, last: current };
  const next = language === 'zh' ? translated(state.source) : state.source;
  state.last = next; textState.set(node, state);
  if (current !== next) node.data = next;
}

function localizeElement(element: Element, language: Language) {
  if (element.closest('svg,code,pre,[data-no-i18n]')) return;
  for (const name of ['placeholder', 'title', 'aria-label']) {
    const current = element.getAttribute(name);
    if (current == null) continue;
    let states = attrState.get(element);
    if (!states) { states = new Map(); attrState.set(element, states); }
    let state = states.get(name);
    if (!state || current !== state.last) state = { source: current, last: current };
    const next = language === 'zh' ? translated(state.source) : state.source;
    state.last = next; states.set(name, state);
    if (current !== next) element.setAttribute(name, next);
  }
}

function applyLanguage(root: Node, language: Language) {
  if (root.nodeType === Node.TEXT_NODE) {
    const parent = (root as Text).parentElement;
    if (parent && !parent.closest('svg,code,pre,[data-no-i18n]')) localizeText(root as Text, language);
    return;
  }
  if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE) return;
  if (root.nodeType === Node.ELEMENT_NODE) localizeElement(root as Element, language);
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  while (node) {
    if (node.nodeType === Node.TEXT_NODE) {
      const parent = (node as Text).parentElement;
      if (parent && !parent.closest('svg,code,pre,[data-no-i18n]')) localizeText(node as Text, language);
    } else localizeElement(node as Element, language);
    node = walker.nextNode();
  }
}

function DomLanguage({ language }: { language: Language }) {
  useLayoutEffect(() => {
    document.documentElement.lang = language === 'zh' ? 'zh-CN' : 'en';
    applyLanguage(document.body, language);
    // ponytail: DOM localization keeps legacy pages source-English; replace
    // with per-component keys only if translators need grammatical variants.
    const observer = new MutationObserver((records) => {
      for (const record of records) {
        if (record.type === 'characterData') applyLanguage(record.target, language);
        for (const node of record.addedNodes) applyLanguage(node, language);
      }
    });
    observer.observe(document.body, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, [language]);
  return null;
}

export function LanguageProvider({ children }: { children: ReactNode }) {
  const [language, setLanguageState] = useState<Language>(() =>
    localStorage.getItem('platform-language') === 'zh' ? 'zh' : 'en');
  const value = useMemo(() => ({ language, setLanguage: (next: Language) => {
    localStorage.setItem('platform-language', next); setLanguageState(next);
  } }), [language]);
  return <LanguageContext.Provider value={value}><DomLanguage language={language} />{children}</LanguageContext.Provider>;
}

export const useLanguage = () => useContext(LanguageContext);
