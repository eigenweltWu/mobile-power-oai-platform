import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { fmtTs } from '../format';
import { Badge, Card, EmptyState, ErrorBox, Field, Icon, Spinner, StatCard, toast } from '../components/ui';

/** 混响室（RC）采样采集：Stirrer 步进 → puschSnr 微调伺服 → 手机定时记录 → 底噪过滤 */

interface Exp {
  experiment_id: string;
  environment: string | null;
  state?: string;
}

interface StirrerStatus {
  simulated: boolean;
  opened: boolean;
  connected: boolean;
  position_deg?: number | null;
  running?: boolean;
  exe_ready?: boolean;
  dll_found?: boolean;
  last_error?: string;
}

interface CampaignStatus {
  running: boolean;
  state: string;
  experiment_id?: string;
  run_id?: string | null;
  samples_done?: number;
  n_steps?: number;
  current_angle_deg?: number | null;
  pusch_x10?: number | null;
  last_rssp_db?: number | null;
  target_rssp_db?: number;
  noise_floor_db?: number | null;
  error?: string | null;
  config?: Record<string, number | boolean>;
  log?: { ms: number; stage: string; msg: string }[];
}

interface RcSample {
  id: number;
  sample_index: number;
  stirrer_angle_deg: number | null;
  pusch_target_snr_x10: number | null;
  rssp_db: number | null;
  target_rssp_db: number | null;
  rssp_error_db: number | null;
  noise_floor_db: number | null;
  tap_count: number | null;
  tap_count_filtered: number | null;
  rms_delay_ns: number | null;
  rms_delay_ns_filtered: number | null;
  peak_db: number | null;
  peak_db_filtered: number | null;
  started_utc_ms: number | null;
  ended_utc_ms: number | null;
  servo_iters: number | null;
  gnb_summary?: Record<string, number> | null;
}

const DEFAULT_CFG = {
  step_deg: 5,
  n_steps: 12,
  dwell_s: 20,
  settle_s: 8,
  target_rssp_db: -60,
  rssp_tol_db: 1.5,
  pusch_step_x10: 10,
  max_servo_iters: 6,
  servo_settle_s: 5,
  noise_frames: 20,
  noise_margin_db: 6,
  simulate_stirrer: false,
};

const STATE_TONE: Record<string, 'good' | 'warn' | 'bad' | 'muted' | 'accent'> = {
  completed: 'good', recording: 'accent', servo: 'accent', moving: 'warn',
  noise_calibration: 'warn', error: 'bad', stopped: 'muted', idle: 'muted',
  preparing: 'warn', finalizing: 'warn',
};

export default function Chamber() {
  const [exps, setExps] = useState<Exp[]>([]);
  const [expId, setExpId] = useState('');
  const [cfg, setCfg] = useState({ ...DEFAULT_CFG });
  const [stirrer, setStirrer] = useState<StirrerStatus | null>(null);
  const [simMode, setSimMode] = useState(false);
  const [camp, setCamp] = useState<CampaignStatus | null>(null);
  const [samples, setSamples] = useState<RcSample[]>([]);
  const [error, setError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const loadExps = useCallback(async () => {
    try {
      const r = await api.get<Exp[] | { experiments: Exp[] }>('/api/experiments');
      if (!mounted.current) return;
      const experiments = Array.isArray(r) ? r : (r.experiments || []);
      setExps(experiments);
      const rc = experiments.find((e) => e.environment === 'RC');
      if (rc && !expId) setExpId(rc.experiment_id);
    } catch (e) { setError(e); }
  }, [expId]);

  const loadStirrer = useCallback(async () => {
    try {
      const r = await api.get<StirrerStatus>(`/api/stirrer/status?simulate=${simMode}`);
      if (mounted.current) setStirrer(r);
    } catch { /* helper offline */ }
  }, [simMode]);

  const loadCampaign = useCallback(async (eid: string) => {
    if (!eid) { setCamp(null); setSamples([]); return; }
    try {
      const [s, c] = await Promise.all([
        api.get<{ samples: RcSample[] }>(`/api/rc/samples?experiment_id=${encodeURIComponent(eid)}`),
        api.get<CampaignStatus>(`/api/rc/campaign/status?experiment_id=${encodeURIComponent(eid)}`),
      ]);
      if (!mounted.current) return;
      setSamples(s.samples || []);
      setCamp(c);
    } catch (e) { setError(e); }
  }, []);

  useEffect(() => { loadExps(); }, [loadExps]);
  useEffect(() => { loadStirrer(); }, [loadStirrer]);

  // live poll while a campaign is active
  useEffect(() => {
    if (!expId) return;
    loadCampaign(expId);
    const active = camp?.running;
    const t = setInterval(() => loadCampaign(expId), active ? 2000 : 10000);
    return () => clearInterval(t);
  }, [expId, camp?.running, loadCampaign]);

  const connect = async () => {
    setBusy(true);
    try {
      const r = await api.post<StirrerStatus>('/api/stirrer/connect', { simulate: simMode });
      setStirrer(r);
      toast('ok', simMode ? '虚拟搅拌器已连接（模拟模式）' : '搅拌器已连接');
    } catch (e) {
      setError(e);
      toast('err', '搅拌器连接失败');
    } finally { setBusy(false); }
  };

  const disconnect = async () => {
    setBusy(true);
    try {
      const r = await api.post<StirrerStatus>('/api/stirrer/disconnect', { simulate: simMode });
      setStirrer(r);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const jog = async (deg: number) => {
    setBusy(true);
    try {
      await api.post('/api/stirrer/move', { deg, simulate: simMode });
      await loadStirrer();
    } catch (e) { setError(e); toast('err', '移动失败'); } finally { setBusy(false); }
  };

  const startCampaign = async () => {
    if (!expId) { toast('err', '先选择实验'); return; }
    setBusy(true);
    try {
      await api.post('/api/rc/campaign/start', {
        experimentId: expId, ...cfg,
      });
      toast('ok', 'RC 采集已启动');
      loadCampaign(expId);
    } catch (e) {
      setError(e);
      toast('err', '启动失败（需先开始实验且手机已 arm）');
    } finally { setBusy(false); }
  };

  const stopCampaign = async () => {
    setBusy(true);
    try {
      await api.post('/api/rc/campaign/stop', { experimentId: expId });
      toast('ok', '已请求停止');
      setTimeout(() => loadCampaign(expId), 1500);
    } catch (e) { setError(e); } finally { setBusy(false); }
  };

  const exp = exps.find((e) => e.experiment_id === expId);
  const active = !!camp?.running;
  const connected = stirrer?.connected || false;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {error ? <ErrorBox error={error} /> : null}

      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))' }}>
        <StatCard icon="settings" label="搅拌器" tone={connected ? 'good' : 'bad'}
          value={connected ? (stirrer?.simulated ? '已连接（模拟）' : '已连接') : '未连接'}
          sub={stirrer?.position_deg != null ? `位置 ${stirrer.position_deg.toFixed(1)}°` : (stirrer?.last_error || 'USB / MT_API')} />
        <StatCard icon="grid" label="实验" tone={exp?.environment === 'RC' ? 'accent' : 'muted'}
          value={exp ? exp.experiment_id : '—'} sub={exp ? `环境 ${exp.environment}` : '选择一个实验'} />
        <StatCard icon="route" label="采集状态" tone={STATE_TONE[camp?.state || 'idle'] ?? 'muted'}
          value={camp?.state || 'idle'}
          sub={camp?.running ? `${camp.samples_done}/${camp.n_steps} 样本` : '未运行'} />
        <StatCard icon="compare" label="RSSP @ gNB"
          value={camp?.last_rssp_db != null ? `${camp.last_rssp_db.toFixed(1)} dB` : '—'}
          sub={camp?.target_rssp_db != null ? `目标 ${camp.target_rssp_db} dB` : ''}
          tone={camp?.last_rssp_db != null && camp?.target_rssp_db != null
            && Math.abs(camp.last_rssp_db - camp.target_rssp_db) <= (cfg.rssp_tol_db || 1.5) ? 'good' : 'warn'} />
      </div>

      <Card title="搅拌器控制（MT_API · USB）"
        sub="真实模式需搅拌器控制器 USB 接入本机；模拟模式用于流程验证"
        right={
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <label style={{ display: 'flex', gap: 4, alignItems: 'center', fontSize: 13 }}>
              <input type="checkbox" checked={simMode} disabled={active}
                onChange={(e) => setSimMode(e.target.checked)} /> 模拟
            </label>
            {connected ? (
              <button className="btn sm" disabled={busy || active} onClick={disconnect}>断开</button>
            ) : (
              <button className="btn sm primary" disabled={busy || active} onClick={connect}>
                <Icon name="play" size={14} /> 连接
              </button>
            )}
          </div>
        }>
        <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center' }}>
          <Badge tone={stirrer?.dll_found ? 'good' : 'bad'}>MT_API.dll {stirrer?.dll_found ? '已找到' : '缺失'}</Badge>
          <Badge tone={stirrer?.exe_ready ? 'good' : 'muted'}>helper {stirrer?.exe_ready ? '已编译' : '未编译'}</Badge>
          <Badge tone={stirrer?.simulated ? 'accent' : 'muted'}>{stirrer?.simulated ? '虚拟电机' : 'USB 电机'}</Badge>
          <span style={{ flex: 1 }} />
          <button className="btn sm" disabled={!connected || busy || active} onClick={() => jog(-cfg.step_deg)}>
            <Icon name="refresh" size={14} /> −{cfg.step_deg}°
          </button>
          <button className="btn sm" disabled={!connected || busy || active} onClick={() => jog(cfg.step_deg)}>
            <Icon name="refresh" size={14} /> +{cfg.step_deg}°
          </button>
        </div>
      </Card>

      <Card title="RC 采集配置"
        sub="每次 Stirrer 步进后：稳定 → 微调 puschTargetSnr 使 gNB 接收 RSSP 回到目标 → 触发手机测速记录 dwell 秒 → 采集 CIR 并按底噪过滤 → 立即停止"
        right={
          active ? (
            <button className="btn sm danger" disabled={busy} onClick={stopCampaign}>
              <Icon name="x" size={14} /> 停止采集
            </button>
          ) : (
            <button className="btn sm primary" disabled={busy || !expId} onClick={startCampaign}>
              <Icon name="play" size={14} /> 启动采集
            </button>
          )
        }>
        <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))', gap: 10 }}>
          <Field label="实验（RC）">
            <select value={expId} onChange={(e) => setExpId(e.target.value)} disabled={active}>
              <option value="">— 选择 —</option>
              {exps.map((e) => (
                <option key={e.experiment_id} value={e.experiment_id}>
                  {e.experiment_id} ({e.environment})
                </option>
              ))}
            </select>
          </Field>
          <Field label="步进角 (°)"><input type="number" step="0.5" value={cfg.step_deg} disabled={active}
            onChange={(e) => setCfg({ ...cfg, step_deg: +e.target.value })} /></Field>
          <Field label="样本数"><input type="number" value={cfg.n_steps} disabled={active}
            onChange={(e) => setCfg({ ...cfg, n_steps: +e.target.value })} /></Field>
          <Field label="记录时长 dwell (s)" hint="手机 loaded 窗口"><input type="number" value={cfg.dwell_s} disabled={active}
            onChange={(e) => setCfg({ ...cfg, dwell_s: +e.target.value })} /></Field>
          <Field label="机械稳定 (s)"><input type="number" value={cfg.settle_s} disabled={active}
            onChange={(e) => setCfg({ ...cfg, settle_s: +e.target.value })} /></Field>
          <Field label="目标 RSSP (dB)"><input type="number" step="0.5" value={cfg.target_rssp_db} disabled={active}
            onChange={(e) => setCfg({ ...cfg, target_rssp_db: +e.target.value })} /></Field>
          <Field label="RSSP 容差 (dB)"><input type="number" step="0.1" value={cfg.rssp_tol_db} disabled={active}
            onChange={(e) => setCfg({ ...cfg, rssp_tol_db: +e.target.value })} /></Field>
          <Field label="pusch 步进 (×0.1dB)"><input type="number" value={cfg.pusch_step_x10} disabled={active}
            onChange={(e) => setCfg({ ...cfg, pusch_step_x10: +e.target.value })} /></Field>
          <Field label="伺服最大次数"><input type="number" value={cfg.max_servo_iters} disabled={active}
            onChange={(e) => setCfg({ ...cfg, max_servo_iters: +e.target.value })} /></Field>
          <Field label="伺服间隔 (s)"><input type="number" value={cfg.servo_settle_s} disabled={active}
            onChange={(e) => setCfg({ ...cfg, servo_settle_s: +e.target.value })} /></Field>
          <Field label="底噪帧数"><input type="number" value={cfg.noise_frames} disabled={active}
            onChange={(e) => setCfg({ ...cfg, noise_frames: +e.target.value })} /></Field>
          <Field label="底噪余量 (dB)"><input type="number" step="0.5" value={cfg.noise_margin_db} disabled={active}
            onChange={(e) => setCfg({ ...cfg, noise_margin_db: +e.target.value })} /></Field>
          <Field label="模拟搅拌器" hint="无硬件时验证全流程">
            <select value={cfg.simulate_stirrer ? '1' : '0'} disabled={active}
              onChange={(e) => setCfg({ ...cfg, simulate_stirrer: e.target.value === '1' })}>
              <option value="0">真实 USB</option>
              <option value="1">模拟</option>
            </select>
          </Field>
        </div>
        {camp?.noise_floor_db != null ? (
          <div style={{ marginTop: 10 }}>
            <Badge tone="accent">底噪 {camp.noise_floor_db.toFixed(1)} dB</Badge>
            {' '}
            <Badge tone="muted">pusch {(camp.pusch_x10 ?? 0) / 10} dB</Badge>
            {camp.run_id ? <Badge tone="muted">run {camp.run_id}</Badge> : null}
          </div>
        ) : null}
      </Card>

      {camp?.running || (camp?.log && camp.log.length > 0 && camp.state !== 'idle') ? (
        <Card title="采集日志" sub={`${camp.state} · ${camp.samples_done}/${camp.n_steps}`}>
          <div className="mono" style={{ maxHeight: 220, overflow: 'auto', fontSize: 12.5, display: 'flex', flexDirection: 'column', gap: 2 }}>
            {(camp.log || []).slice().reverse().map((l, i) => (
              <div key={i}>
                <span style={{ opacity: 0.55 }}>[{new Date(l.ms).toLocaleTimeString()}] {l.stage}</span> {l.msg}
              </div>
            ))}
          </div>
        </Card>
      ) : null}

      <Card title="RC 样本历史" sub="每个样本 = 一次 Stirrer 步进（区别于 AC 的连续记录 + 剪辑）">
        {samples.length === 0 ? (
          <EmptyState>暂无样本 — 启动采集后每步 Stirrer 会生成一条</EmptyState>
        ) : (
          <div style={{ overflowX: 'auto' }}>
            <table className="table" style={{ minWidth: 900 }}>
              <thead>
                <tr>
                  <th>#</th><th>角度°</th><th>pusch dB</th><th>RSSP dB</th><th>误差 dB</th>
                  <th>底噪 dB</th><th>taps (原始/过滤)</th><th>RMS 延迟 ns (过滤)</th>
                  <th>峰值 dB</th><th>窗口</th><th>伺服</th><th>gNB UL Mbps / BLER</th>
                </tr>
              </thead>
              <tbody>
                {samples.map((s) => (
                  <tr key={s.id}>
                    <td className="mono">{s.sample_index}</td>
                    <td className="mono">{s.stirrer_angle_deg?.toFixed(1) ?? '—'}</td>
                    <td className="mono">{s.pusch_target_snr_x10 != null ? (s.pusch_target_snr_x10 / 10).toFixed(1) : '—'}</td>
                    <td className="mono">{s.rssp_db?.toFixed(1) ?? '—'}</td>
                    <td className="mono" style={{ color: Math.abs(s.rssp_error_db ?? 99) > cfg.rssp_tol_db ? '#e07a5f' : undefined }}>
                      {s.rssp_error_db != null ? (s.rssp_error_db > 0 ? '+' : '') + s.rssp_error_db.toFixed(1) : '—'}
                    </td>
                    <td className="mono">{s.noise_floor_db?.toFixed(1) ?? '—'}</td>
                    <td className="mono">{s.tap_count ?? '—'} / {s.tap_count_filtered ?? '—'}</td>
                    <td className="mono">{s.rms_delay_ns_filtered?.toFixed(0) ?? '—'}</td>
                    <td className="mono">{s.peak_db_filtered?.toFixed(1) ?? '—'}</td>
                    <td className="mono" style={{ fontSize: 11.5 }}>
                      {s.started_utc_ms ? fmtTs(s.started_utc_ms) : '—'}<br />
                      {(s.ended_utc_ms && s.started_utc_ms) ? `+${((s.ended_utc_ms - s.started_utc_ms) / 1000).toFixed(1)}s` : ''}
                    </td>
                    <td className="mono">{s.servo_iters ?? '—'}</td>
                    <td className="mono">
                      {s.gnb_summary?.ul != null ? s.gnb_summary.ul.toFixed(1) : '—'}
                      {' / '}
                      {s.gnb_summary?.bler != null ? s.gnb_summary.bler.toFixed(3) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
