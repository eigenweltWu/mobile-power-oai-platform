import { Fragment, useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Experiment, PlatformStatus } from '../types';
import { Badge, Card, ErrorBox, Spinner, StatCard, toast } from '../components/ui';
import { fmtBytes } from '../format';

const DEFAULT_COLLECTION_SECONDS = 120;

/* ------------------------------------------------------------------ */
/* OAI radio detail (extracted, never raw JSON dumps)                 */
/* ------------------------------------------------------------------ */

interface OaiDetail {
  radio: Record<string, unknown> | null;      // /api/oai/status → radio
  gnbRunning: boolean | null;
  ueCount: number;
  ulScheduler: Record<string, unknown> | null; // /api/oai/controls → ulScheduler
  puschTarget: Record<string, unknown> | null; // /api/oai/controls → puschTarget
}

function fmtNum(v: unknown, unit = '', digits = 1): string {
  if (v === null || v === undefined || v === '') return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return String(v);
  if (Number.isInteger(n)) return `${n}${unit}`;
  return `${n.toFixed(digits)}${unit}`;
}

/** Parse a config JSON string; never throws. */
function parseConfig(json: string | null | undefined): Record<string, unknown> | null {
  if (!json) return null;
  try { return JSON.parse(json) as Record<string, unknown>; } catch { return null; }
}

/** Compact key/value rendering of an OAI config (known keys first, rest after). */
const CONFIG_KEY_ORDER = [
  'frequencyMHz', 'bandwidthMHz', 'txGainDb', 'rxGainDb',
  'puschTargetMode', 'puschTargetSnrX10', 'schedulerMode', 'mcs', 'qm', 'nPrb',
];

function ConfigFields({ cfg }: { cfg: Record<string, unknown> | null }) {
  if (!cfg) return <div style={{ fontSize: 12, color: 'var(--muted)' }}>（无配置字段）</div>;
  const keys = [
    ...CONFIG_KEY_ORDER.filter((k) => cfg[k] !== undefined && cfg[k] !== null),
    ...Object.keys(cfg).filter((k) => !CONFIG_KEY_ORDER.includes(k) && cfg[k] !== null && cfg[k] !== undefined),
  ];
  if (!keys.length) return <div style={{ fontSize: 12, color: 'var(--muted)' }}>（无配置字段）</div>;
  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'auto 1fr', gap: '2px 10px', fontSize: 12 }}>
      {keys.map((k) => (
        <Fragment key={k}>
          <span style={{ color: 'var(--muted)' }}>{k}</span>
          <span style={{ fontFamily: 'var(--mono)' }}>{String(cfg[k])}</span>
        </Fragment>
      ))}
    </div>
  );
}

/** 关键可调参数（apply_condition 支持的 keys）分三组展示。 */
function OaiParamsCard({ detail, onRefresh }: { detail: OaiDetail; onRefresh: () => void }) {
  const r = detail.radio ?? {};
  const ul = detail.ulScheduler ?? {};
  const pt = detail.puschTarget ?? {};
  const row = (label: string, value: string, tag?: string) => (
    <Fragment key={label}>
      <dt>{label}</dt>
      <dd>
        {value}
        {tag ? <span className="badge muted" style={{ marginLeft: 8 }}>{tag}</span> : null}
      </dd>
    </Fragment>
  );
  return (
    <Card
      title="OAI Radio 设置"
      sub={`当前 gNB 生效配置 · UE 数 ${detail.ueCount}`}
      right={<button className="btn" onClick={onRefresh}>刷新</button>}
    >
      <div className="grid cols-3" style={{ rowGap: 24 }}>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--muted)' }}>射频（可调）</div>
          <div className="kv">
            {row('frequencyMHz', fmtNum(r.frequencyMHz, ' MHz'))}
            {row('bandwidthMHz', fmtNum(r.bandwidthMHz, ' MHz'))}
            {row('txGainDb', fmtNum(r.txGainDb, ' dB'))}
            {row('rxGainDb', fmtNum(r.rxGainDb, ' dB'))}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--muted)' }}>PUSCH 功控（可调）</div>
          <div className="kv">
            {row('puschTargetMode', String(pt.mode ?? '—'))}
            {row('puschTargetSnrX10', fmtNum(pt.targetSnrX10, '', 0))}
            {row('targetSnrDb', fmtNum(pt.targetSnrDb, ' dB'))}
          </div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, marginTop: 16, color: 'var(--muted)' }}>UL 调度器（可调）</div>
          <div className="kv">
            {row('schedulerMode', String(ul.mode ?? '—'))}
            {row('mcs', fmtNum(ul.mcs, '', 0))}
            {row('qm', fmtNum(ul.qm, '', 0))}
            {row('nPrb', fmtNum(ul.nPrb, '', 0))}
          </div>
        </div>
        <div>
          <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 8, color: 'var(--muted)' }}>载波信息（只读）</div>
          <div className="kv">
            {row('band', fmtNum(r.band, '', 0))}
            {row('arfcn', fmtNum(r.arfcn, '', 0))}
            {row('carrierPrb', fmtNum(r.carrierPrb, ' PRB', 0))}
            {row('subcarrierSpacingKhz', fmtNum(r.subcarrierSpacingKhz, ' kHz', 0))}
            {row('supportedBandwidthMHz', Array.isArray(r.supportedBandwidthMHz) ? (r.supportedBandwidthMHz as number[]).join(' / ') + ' MHz' : '—')}
          </div>
        </div>
      </div>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Throughput · rolling 1-minute chart (hand-rolled SVG, no deps)      */
/* ------------------------------------------------------------------ */

interface ThpPoint { t: number; dl: number; ul: number }

function ThpChart({ history }: { history: ThpPoint[] }) {
  const W = 640, H = 180, PAD_L = 44, PAD_B = 20, PAD_T = 10, PAD_R = 10;
  const now = history[history.length - 1].t;
  const t0 = now - 60_000;
  const maxV = Math.max(1, ...history.map((p) => Math.max(p.dl, p.ul)));
  const yMax = Math.ceil(maxV * 1.15);
  const x = (t: number) => PAD_L + ((t - t0) / 60_000) * (W - PAD_L - PAD_R);
  const y = (v: number) => PAD_T + (1 - v / yMax) * (H - PAD_T - PAD_B);
  const path = (key: 'dl' | 'ul') => history.map((p, i) => `${i === 0 ? 'M' : 'L'}${x(p.t).toFixed(1)},${y(p[key]).toFixed(1)}`).join(' ');
  const yTicks = [0, yMax / 2, yMax];
  return (
    <div>
      <div className="row" style={{ gap: 16, fontSize: 12, marginBottom: 6 }}>
        <span className="row" style={{ gap: 6 }}><span style={{ width: 10, height: 3, background: 'var(--accent)' }} />DL Mbps</span>
        <span className="row" style={{ gap: 6 }}><span style={{ width: 10, height: 3, background: '#2e9e5b' }} />UL Mbps</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }} role="img">
        {yTicks.map((v) => (
          <g key={v}>
            <line x1={PAD_L} x2={W - PAD_R} y1={y(v)} y2={y(v)} stroke="var(--border)" strokeWidth={1} />
            <text x={PAD_L - 6} y={y(v) + 4} textAnchor="end" fontSize={10} fill="var(--muted)">{v.toFixed(0)}</text>
          </g>
        ))}
        {[0, 15, 30, 45, 60].map((s) => (
          <text key={s} x={x(now - s * 1000)} y={H - 6} textAnchor="middle" fontSize={10} fill="var(--muted)">-{s}s</text>
        ))}
        <path d={path('dl')} fill="none" stroke="var(--accent)" strokeWidth={2} />
        <path d={path('ul')} fill="none" stroke="#2e9e5b" strokeWidth={2} />
      </svg>
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Dashboard                                                          */
/* ------------------------------------------------------------------ */

export default function Dashboard() {
  const [status, setStatus] = useState<PlatformStatus | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);

  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [selectedExperimentId, setSelectedExperimentId] = useState<string>('');
  const [collectionSeconds, setCollectionSeconds] = useState<number>(DEFAULT_COLLECTION_SECONDS);

  const [templates, setTemplates] = useState<Array<{ id: number; name: string; config_json: string; created_utc: string }>>([]);
  const [selectedTemplate, setSelectedTemplate] = useState<string>('');

  const [oai, setOai] = useState<OaiDetail>({ radio: null, gnbRunning: null, ueCount: 0, ulScheduler: null, puschTarget: null });

  const [starting, setStarting] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [applying, setApplying] = useState(false);

  /** Rolling 1-minute throughput samples (pushed on every status poll). */
  const [thpHistory, setThpHistory] = useState<Array<{ t: number; dl: number; ul: number }>>([]);

  /* ---- loading ---- */
  const loadStatus = useCallback(() => {
    api.get<PlatformStatus>('/api/platform/status')
      .then((s) => {
        setStatus(s); setError(null); setLastUpdated(Date.now());
        const tp = s.oai?.throughput;
        if (tp) {
          const now = Date.now();
          setThpHistory((prev) => [...prev, { t: now, dl: tp.dlMbps, ul: tp.ulMbps }]
            .filter((p) => now - p.t <= 60_000));
        }
      })
      .catch((e) => { setError(e instanceof Error ? e : new Error(String(e))); });
  }, []);

  const loadExperiments = useCallback(() => {
    api.get<Experiment[]>('/api/experiments')
      .then((list) => {
        setExperiments(list);
        setSelectedExperimentId((prev) => (prev && list.some((e) => e.experiment_id === prev)) ? prev : (list[0]?.experiment_id ?? ''));
      })
      .catch(() => {});
  }, []);

  const loadOai = useCallback(() => {
    Promise.allSettled([
      api.get<Record<string, unknown>>('/api/oai/status'),
      api.get<Record<string, unknown>>('/api/oai/controls'),
    ]).then(([s, c]) => {
      const st = s.status === 'fulfilled' ? s.value : null;
      const ct = c.status === 'fulfilled' ? c.value : null;
      const ues = (st?.ues ?? []) as unknown[];
      setOai({
        radio: (st?.radio ?? null) as Record<string, unknown> | null,
        gnbRunning: st ? Boolean((st.gnb as Record<string, unknown> | undefined)?.running) : null,
        ueCount: Array.isArray(ues) ? ues.length : 0,
        ulScheduler: (ct?.ulScheduler ?? null) as Record<string, unknown> | null,
        puschTarget: (ct?.puschTarget ?? null) as Record<string, unknown> | null,
      });
    });
  }, []);

  useEffect(() => {
    loadStatus();
    loadExperiments();
    loadOai();
    const t = setInterval(loadStatus, 3000);
    return () => clearInterval(t);
  }, [loadStatus, loadExperiments, loadOai]);

  /* ---- templates: default-select the one matching the stored startup
     config (initial_oai_config is itself one of the templates), else the
     first template. There is no separate "initial config" pseudo-entry. ---- */
  const selectedExperiment = useMemo(
    () => experiments.find((e) => e.experiment_id === selectedExperimentId) ?? null,
    [experiments, selectedExperimentId],
  );

  const initialConfig = useMemo(() => {
    if (!selectedExperiment?.initial_oai_config) return null;
    try { return JSON.parse(selectedExperiment.initial_oai_config) as Record<string, unknown>; } catch { return null; }
  }, [selectedExperiment]);

  useEffect(() => {
    if (!selectedExperimentId) { setTemplates([]); return; }
    api.get<Array<{ id: number; name: string; config_json: string; created_utc: string }>>(
      `/api/experiments/${encodeURIComponent(selectedExperimentId)}/templates`,
    ).then((list) => {
      setTemplates(list);
      setSelectedTemplate((prev) => {
        if (prev && list.some((t) => String(t.id) === prev)) return prev;
        if (initialConfig) {
          const match = list.find((t) => {
            try {
              const cfg = JSON.parse(t.config_json || '{}') as Record<string, unknown>;
              return JSON.stringify(cfg) === JSON.stringify(initialConfig);
            } catch { return false; }
          });
          if (match) return String(match.id);
        }
        return list[0] ? String(list[0].id) : '';
      });
    }).catch(() => setTemplates([]));
  }, [selectedExperimentId, initialConfig]);

  /* ---- actions ---- */
  const startExperiment = async () => {
    if (!selectedExperimentId) { toast('err', '请先选择实验'); return; }
    setStarting(true);
    try {
      const r = await api.post<{ gnb_running?: boolean; ue_in_sync?: boolean; run_id?: string }>(
        `/api/experiments/${encodeURIComponent(selectedExperimentId)}/start`,
        {
          collection_seconds: Number(collectionSeconds),
          template_id: selectedTemplate === '' ? null : Number(selectedTemplate),
        },
      );
      const tplName = templates.find((t) => String(t.id) === selectedTemplate)?.name;
      const parts: string[] = ['下行探测已启动'];
      if (tplName) parts.push(`模板「${tplName}」`);
      if (r.gnb_running) parts.push('gNB 运行中');
      if (r.ue_in_sync) parts.push('UE 同步');
      toast('ok', `${parts.join('·')}·请手机点击「开始实验」`);
      // Optimistic update: show the stop button immediately.
      if (r.run_id) {
        setStatus((prev) => (prev ? {
          ...prev,
          experiment: { latest_run: { run_id: r.run_id as string, experiment_id: selectedExperimentId, condition_id: '', state: 'PREPARING', last_error: null } },
        } : prev));
      }
      setThpHistory([]);
      loadStatus(); loadOai();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally { setStarting(false); }
  };

  const stopExperiment = async () => {
    const eid = status?.experiment.latest_run?.experiment_id ?? selectedExperimentId;
    if (!eid) { toast('err', '没有可停止的实验'); return; }
    setStopping(true);
    try {
      const r = await api.post<{ pc_stop_ms?: number; phone_stop_ms?: number }>(
        `/api/experiments/${encodeURIComponent(eid)}/stop`, {});
      const parts: string[] = [];
      if (r.pc_stop_ms) parts.push(`PC ${new Date(r.pc_stop_ms).toLocaleTimeString()}`);
      if (r.phone_stop_ms) parts.push(`手机 ${new Date(r.phone_stop_ms).toLocaleTimeString()}`);
      toast('ok', parts.length ? `已停止·${parts.join('，')}` : '已停止');
      // Optimistic update: flip to the start button immediately (the stop
      // request itself may take seconds while the backend probes the phone).
      setStatus((prev) => (prev && prev.experiment.latest_run
        ? { ...prev, experiment: { latest_run: { ...prev.experiment.latest_run, state: 'STOPPED' } } }
        : prev));
      setThpHistory([]);
      loadStatus();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally { setStopping(false); }
  };

  const applyTemplate = async () => {
    if (selectedTemplate === '') { toast('err', '请先选择 Template'); return; }
    setApplying(true);
    try {
      const ph = (status?.phone ?? null) as Record<string, unknown> | null;
      await api.post(
        `/api/experiments/${encodeURIComponent(selectedExperimentId)}/templates/${selectedTemplate}/apply`,
        { serial: (ph?.serial as string) || '53616213', pc_port: (ph?.pc_port as number) || 8420 },
      );
      toast('ok', 'Template 已应用·gNB 重启后将重新触发 idle→loaded→idle');
      loadOai(); loadStatus();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally { setApplying(false); }
  };

  /* ---- render ---- */
  if (!status && !error) return <Spinner label="loading platform status…" />;
  if (error && !status) return <ErrorBox error={error} />;
  if (!status) return null;

  const phone = status.phone;
  const phoneConnected = phone.state?.toUpperCase() === 'CONNECTED';
  const phoneAttached = phone.usb_attached === true;
  const latestRun = status.experiment.latest_run ?? null;
  const storage = status.storage ?? { n_files: 0, bytes: 0 };
  const phonePhase = ((phone.status ?? null) as Record<string, unknown> | null)?.phase;

  const runningStates = ['PREPARING', 'ARMED', 'RUNNING'];
  const isRunning = !!latestRun && runningStates.includes(latestRun.state);
  const selectedTemplateName = templates.find((t) => String(t.id) === selectedTemplate)?.name;
  // Template switch is only allowed in the idle phase (or with no run in
  // flight). The backend re-checks this — here we just disable the button
  // early so the operator gets immediate feedback.
  const switchBlocked = isRunning && phonePhase !== 'IDLE';

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Platform Dashboard</div>
          <div className="subtitle">
            {lastUpdated ? `updated ${new Date(lastUpdated).toLocaleTimeString()}` : 'OAI gNB · 手机 · 空口对时 实时总览'}
          </div>
        </div>
        <button className="btn" onClick={() => { loadStatus(); loadOai(); }}>Refresh</button>
      </div>

      {error && <ErrorBox error={error} />}

      <div className="stat-grid">
        <StatCard
          label="Phone"
          value={
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {phoneConnected ? <Badge tone="good">CONNECTED</Badge> : null}
              {!phoneConnected && phoneAttached ? <Badge tone="accent">ATTACHED</Badge> : null}
              {!phoneConnected && !phoneAttached ? <Badge tone="muted">OFFLINE</Badge> : null}
            </div>
          }
          sub={phone.device ?? 'no USB, no 5G link'}
          tone={phoneConnected ? 'good' : phoneAttached ? 'accent' : 'muted'}
          icon="play"
        />
        <StatCard
          label="OAI Core"
          value={status.oai.healthy ? 'HEALTHY' : 'UNREACHABLE'}
          sub={`${status.oai.gnb_running ? 'gNB running' : 'gNB stopped'} · ${status.oai.ue_in_sync ? 'UE in-sync' : 'UE out-of-sync'}`}
          tone={status.oai.healthy ? 'good' : 'bad'}
          icon="flask"
        />
        <StatCard label="Storage" value={fmtBytes(storage.bytes)} sub={`${storage.n_files} files`} tone="accent" icon="data" />
      </div>

      {/* ---- active experiment ---- */}
      <Card
        title="Active Experiment"
        sub="选择实验 · 采集时间 · 开始/停止 · 空口握手进度"
        right={
          isRunning ? (
            <button className="btn danger" disabled={stopping} onClick={stopExperiment}>
              {stopping ? 'Stopping…' : '停止实验'}
            </button>
          ) : (
            <button className="btn primary" disabled={starting || !selectedExperimentId} onClick={startExperiment}>
              {starting ? 'Starting…' : '开始实验'}
            </button>
          )
        }
      >
        <div className="grid cols-2">
          <div className="kv">
            <dt>实验</dt>
            <dd>
              <select className="field" style={{ width: '100%' }} value={selectedExperimentId}
                disabled={isRunning}
                onChange={(e) => setSelectedExperimentId(e.target.value)}>
                <option value="">— 选择 —</option>
                {experiments.map((e) => (
                  <option key={e.experiment_id} value={e.experiment_id}>{e.experiment_id}</option>
                ))}
              </select>
            </dd>
            <dt>采集时间（秒）</dt>
            <dd>
              <input type="number" className="field" style={{ width: 160 }} min={1} disabled={isRunning}
                value={collectionSeconds} onChange={(e) => setCollectionSeconds(Number(e.target.value))} />
            </dd>
            <dt>阶段计划</dt>
            <dd style={{ fontSize: 12, color: 'var(--muted)' }}>
              idle(空载) → loaded({collectionSeconds}s 满载) → idle(持续记录到停止)
            </dd>
          </div>
          <div className="kv">
            <dt>状态</dt>
            <dd>
              {latestRun ? (
                <span className="row" style={{ gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                  <Badge tone={latestRun.state === 'FAILED' ? 'bad' : isRunning ? 'good' : 'muted'}>{latestRun.state}</Badge>
                  <code style={{ fontSize: 12 }}>{latestRun.run_id}</code>
                </span>
              ) : <span style={{ color: 'var(--muted)' }}>暂无</span>}
            </dd>
            {phonePhase ? (
              <>
                <dt>手机当前阶段</dt>
                <dd>{String(phonePhase)}</dd>
              </>
            ) : null}
            {status.clock.delay_ms != null && isRunning ? (
              <>
                <dt>通信时延</dt>
                <dd>{status.clock.delay_ms.toFixed(1)} ms</dd>
              </>
            ) : null}
            {latestRun && latestRun.state === 'FAILED' && latestRun.last_error ? (
              <>
                <dt>error</dt>
                <dd style={{ color: 'var(--bad)', fontSize: 12 }}>{latestRun.last_error}</dd>
              </>
            ) : null}
          </div>
        </div>
      </Card>

      {/* ---- live throughput (rolling 1 minute) ---- */}
      <Card
        title="Throughput · 近 1 分钟"
        sub={(() => {
          const last = thpHistory[thpHistory.length - 1];
          return last ? `DL ${last.dl.toFixed(2)} Mbps · UL ${last.ul.toFixed(2)} Mbps · ${thpHistory.length} 采样` : '实验运行后开始记录';
        })()}
      >
        {thpHistory.length < 2 ? (
          <div className="empty-state">暂无吞吐数据（gNB 运行并接入 UE 后显示）</div>
        ) : (
          <ThpChart history={thpHistory} />
        )}
      </Card>

      {/* ---- OAI radio key params (structured, editable keys only) ---- */}
      <OaiParamsCard detail={oai} onRefresh={loadOai} />

      {/* ---- OAI template (selectable card list) ---- */}
      <Card
        title="OAI Template"
        sub={`点击卡片选择 · 当前选中：${selectedTemplateName ?? '无'} · 开始实验时应用所选模板${switchBlocked ? ` · 仅 idle 阶段可切换（当前：${phonePhase ?? '未知'}）` : ''}`}
        right={
          <button
            className="btn"
            disabled={applying || selectedTemplate === '' || switchBlocked}
            onClick={applyTemplate}
            title={switchBlocked ? '实验运行中，仅 idle 阶段可切换 Template' : undefined}
          >
            {applying ? '切换中…' : '切换（重启 gNB）'}
          </button>
        }
      >
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          {templates.map((t) => {
            const selected = selectedTemplate === String(t.id);
            return (
              <div
                key={t.id}
                onClick={() => setSelectedTemplate(String(t.id))}
                style={{
                  cursor: 'pointer', padding: '10px 14px', borderRadius: 'var(--radius-sm)',
                  border: `2px solid ${selected ? 'var(--accent)' : 'var(--border)'}`,
                  background: selected ? 'var(--accent-soft)' : 'var(--panel)',
                }}
              >
                <div className="row" style={{ justifyContent: 'space-between', marginBottom: 6 }}>
                  <strong style={{ fontSize: 13 }}>{t.name}</strong>
                  {selected ? <span className="badge accent" style={{ fontSize: 11 }}>已选中</span> : null}
                </div>
                <ConfigFields cfg={parseConfig(t.config_json)} />
              </div>
            );
          })}
          {!templates.length ? (
            <div className="empty-state">本实验尚未添加 Template，请在 Experiments 页编辑任务添加。</div>
          ) : null}
        </div>
      </Card>

      <Card title="Legend">
        <div className="row" style={{ fontSize: 12, color: 'var(--muted)', gap: 16, flexWrap: 'wrap' }}>
          <div className="row" style={{ gap: 6 }}><Badge tone="good">CONNECTED</Badge><span>手机 agent 可达（USB 或 5G 控制通道）</span></div>
          <div className="row" style={{ gap: 6 }}><Badge tone="accent">ATTACHED</Badge><span>USB 已连接·agent 未启动</span></div>
          <div className="row" style={{ gap: 6 }}><Badge tone="muted">OFFLINE</Badge><span>控制通道均不通</span></div>
        </div>
        <div style={{ fontSize: 12, color: 'var(--muted)', marginTop: 8 }}>
          对时握手通过 5G 空口完成（PC 下行探测 → 手机空口 ACK → sync-confirm），USB 仅作为控制/数据通道。
        </div>
      </Card>
    </div>
  );
}
