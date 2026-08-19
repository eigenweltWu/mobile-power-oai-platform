import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Experiment, PlatformStatus } from '../types';
import { Badge, Card, ErrorBox, Spinner, StatCard, toast } from '../components/ui';
import { fmtBytes } from '../format';
import { useOperatorContext } from '../context';
import { Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

type Configuration = { id: number; name: string; config_json: string; is_default?: number; version?: number };
type Check = { group: string; key: string; ok: boolean; label: string; action?: string | null };
type Applied = { configuration_id: number; configuration_name: string; configuration_version: number; status: string; actual_config?: Record<string, unknown>; diff?: Record<string, { requested: unknown; actual: unknown }> };
type Preflight = { ready: boolean; checks: Check[]; issues: Check[]; execution_mode: string; selected: { id: number; name: string; version: number; config: Record<string, any> }; applied: Applied | null };
type Campaign = { running: boolean; state: string; samples_done?: number; n_steps?: number; current_angle_deg?: number | null; last_rssp_db?: number | null; log?: { ms: number; stage: string; msg: string }[] };
type Runtime = {
  gnb_state: string; environment: string; run?: { run_id: string; state: string } | null;
  ac: { state: string; phase_index?: number; phase_count?: number; phase?: { name: string; durationSeconds: number; configurationId?: number }; phase_remaining_s?: number | null; configuration_name?: string };
  rc: Campaign & { current_sample_index?: number; target_rssp_db?: number; current_tx_gain_db?: number; pusch_x10?: number; calibration_records?: Array<{ ms: number; sample_index: number; rssp_db: number; target_rssp_db: number; tx_gain_db?: number; target_snr_db?: number }> };
  series: Array<{ time: number; throughput?: number | null; rssp?: number | null; snr?: number | null }>;
};

const RUNNING = ['PREPARING', 'ARMED', 'RUNNING', 'STOPPING'];
const parse = (json: string) => { try { return JSON.parse(json) as Record<string, any>; } catch { return {}; } };

export default function Dashboard({ nav }: { nav: (path: string) => void }) {
  const { value: context, update: updateContext } = useOperatorContext();
  const [status, setStatus] = useState<PlatformStatus | null>(null);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [experimentId, setExperimentId] = useState(context.experimentId);
  const [configurations, setConfigurations] = useState<Configuration[]>([]);
  const [configurationId, setConfigurationId] = useState<number | null>(context.configurationId);
  const [configurationExperimentId, setConfigurationExperimentId] = useState('');
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [busy, setBusy] = useState<'apply' | 'start' | 'stop' | ''>('');
  const [error, setError] = useState<Error | null>(null);

  const experiment = experiments.find((row) => row.experiment_id === experimentId);
  const configuration = configurations.find((row) => row.id === configurationId);
  const latestRun = status?.experiment.latest_run;
  const running = !!latestRun && RUNNING.includes(latestRun.state);
  const isRc = experiment?.environment === 'RC';

  const load = useCallback(async () => {
    try {
      const [platform, list] = await Promise.all([api.get<PlatformStatus>('/api/platform/status'), api.get<Experiment[]>('/api/experiments')]);
      setStatus(platform); setExperiments(list); setError(null);
      setExperimentId((old) => old && list.some((row) => row.experiment_id === old) ? old : (list[0]?.experiment_id || ''));
    } catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
  }, []);
  useEffect(() => { load(); const timer = window.setInterval(load, 3000); return () => window.clearInterval(timer); }, [load]);

  useEffect(() => {
    if (!experimentId) return;
    let cancelled = false;
    setConfigurationExperimentId(''); setConfigurations([]); setConfigurationId(null); setPreflight(null);
    api.get<Configuration[]>(`/api/experiments/${encodeURIComponent(experimentId)}/templates`).then((rows) => {
      if (cancelled) return;
      setConfigurationExperimentId(experimentId);
      setConfigurations(rows);
      setConfigurationId((old) => rows.some((row) => row.id === old) ? old : (rows.find((row) => row.is_default)?.id ?? rows[0]?.id ?? null));
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause : new Error(String(cause))); });
    return () => { cancelled = true; };
  }, [experimentId]);

  const loadPreflight = useCallback(async () => {
    if (!experimentId || configurationExperimentId !== experimentId || configurationId == null) { setPreflight(null); return; }
    try { setPreflight(await api.get(`/api/run-control/preflight?experiment_id=${encodeURIComponent(experimentId)}&configuration_id=${configurationId}`)); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
  }, [experimentId, configurationExperimentId, configurationId]);
  useEffect(() => { loadPreflight(); const timer = window.setInterval(loadPreflight, 4000); return () => window.clearInterval(timer); }, [loadPreflight]);

  useEffect(() => {
    if (experiment?.environment !== 'RC') { setCampaign(null); return; }
    const poll = () => api.get<Campaign>(`/api/rc/campaign/status?experiment_id=${encodeURIComponent(experimentId)}`).then(setCampaign).catch(() => undefined);
    poll(); const timer = window.setInterval(poll, 2000); return () => window.clearInterval(timer);
  }, [experimentId, experiment?.environment]);
  useEffect(() => {
    if (!experimentId) return;
    const poll = () => api.get<Runtime>(`/api/experiments/${encodeURIComponent(experimentId)}/runtime`).then(setRuntime).catch(() => undefined);
    poll(); const timer = window.setInterval(poll, 2000); return () => window.clearInterval(timer);
  }, [experimentId]);

  useEffect(() => updateContext({ experimentId, environment: experiment?.environment || '', configurationId,
    configurationName: isRc ? 'RC Workflow' : configuration ? `${configuration.name} v${configuration.version ?? 1}` : '',
    runId: latestRun?.experiment_id === experimentId ? latestRun.run_id : '',
    status: latestRun?.experiment_id === experimentId ? latestRun.state : '',
  }), [experimentId, experiment?.environment, configurationId, configuration, latestRun, isRc, updateContext]);

  const apply = async () => {
    if (configurationId == null) return;
    setBusy('apply'); setError(null);
    try {
      const result = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/templates/${configurationId}/apply`, { serial: status?.phone.serial || '' });
      if (result.verification_status === 'VERIFIED') toast('ok', isRc ? 'Workflow applied and verified.' : 'Configuration applied and verified.');
      else setError(new Error(`Applied with differences: ${JSON.stringify(result.diff)}`));
      await Promise.all([load(), loadPreflight()]);
    } catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
    finally { setBusy(''); }
  };
  const start = async () => {
    if (!preflight?.ready || configurationId == null) return;
    setBusy('start'); setError(null);
    try {
      const result = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/start`, { template_id: configurationId, serial: status?.phone.serial || '' });
      updateContext({ runId: result.run_id, status: 'PREPARING' });
      toast('ok', result.rc_campaign_started ? 'Run started; RC is waiting for post-restart UE sync.' : 'gNB restarted; waiting for post-restart UE sync.');
      await load();
    } catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
    finally { setBusy(''); }
  };
  const stop = async () => {
    setBusy('stop'); setError(null); updateContext({ status: 'STOPPING' });
    try { await api.post(`/api/experiments/${encodeURIComponent(latestRun?.experiment_id || experimentId)}/stop`, { serial: status?.phone.serial || '' }); toast('ok', 'Run stopped after backend confirmation.'); await load(); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
    finally { setBusy(''); }
  };

  const groups = useMemo(() => Object.entries((preflight?.checks || []).reduce((out, check) => ({ ...out, [check.group]: [...(out[check.group] || []), check] }), {} as Record<string, Check[]>)), [preflight]);
  if (!status) return error ? <ErrorBox error={error} /> : <Spinner label="Loading Run Control…" />;
  const requested = preflight?.selected.config || (configuration ? parse(configuration.config_json) : {});
  const applied = preflight?.applied;
  const selectedApplied = applied?.configuration_id === configurationId &&
    applied.configuration_version === (configuration?.version ?? 1) &&
    applied.status === 'VERIFIED';
  const displayRunTerm = (value: string) => isRc
    ? value.replace(/\bConfigurations\b/g, 'Workflow').replace(/\bConfiguration\b/g, 'Workflow').replace(/\bconfigurations\b/g, 'workflow').replace(/\bconfiguration\b/g, 'workflow')
    : value;

  return <div className="stack">
    <div className="page-head"><div><div className="title">Run Control</div><div className="subtitle">{isRc ? 'Review the complete RC Workflow, check external dependencies, then Start.' : 'Select a Configuration, check external dependencies, then Start. Every Start applies it and force-restarts gNB.'}</div></div><button className="btn" onClick={() => { load(); loadPreflight(); }}>Refresh</button></div>
    {error && <ErrorBox error={error} />}
    <div className="stat-grid"><StatCard label="Phone" value={status.phone.state} sub={status.phone.device || 'Device unavailable'} tone={status.phone.state === 'CONNECTED' ? 'good' : 'bad'} /><StatCard label="gNB Current State" value={status.oai.gnb_running ? 'RUNNING' : 'STOPPED'} sub={isRc ? 'Start always reapplies the Workflow and restarts gNB' : 'Start always applies Selected and restarts gNB'} tone={status.oai.gnb_running ? 'good' : 'muted'} /><StatCard label="UE Sync" value={status.oai.ue_in_sync ? 'CURRENTLY SYNCED' : 'WAITING'} sub="Established after Start and recorded as the Run time anchor" tone={status.oai.ue_in_sync ? 'good' : 'muted'} /><StatCard label="Storage" value={fmtBytes(status.storage.bytes)} sub={`${status.storage.n_files} indexed files`} tone="muted" /></div>

    <Card title="Run Context" sub="A fresh Run ID and standard timing values are generated automatically on every Start."><div className="grid cols-2"><label className="field"><span>Experiment</span><select value={experimentId} disabled={running} onChange={(event) => setExperimentId(event.target.value)}>{experiments.map((row) => <option key={row.experiment_id} value={row.experiment_id}>{row.experiment_id} · {row.environment}</option>)}</select></label>{!isRc && <label className="field"><span>Selected for next Run</span><select value={configurationId ?? ''} disabled={running} onChange={(event) => setConfigurationId(Number(event.target.value))}>{configurations.map((row) => <option key={row.id} value={row.id}>{row.name} v{row.version ?? 1}{row.is_default ? ' · Default' : ''}</option>)}</select></label>}{isRc && <div className="field"><span>Workflow</span><b>RC Workflow</b></div>}</div></Card>

    {!isRc && <Card title="Configuration State" sub="Applied is informational. Start always reapplies Selected and force-restarts gNB." right={<button className="btn" disabled={busy !== '' || running || configurationId == null} onClick={apply}>{busy === 'apply' ? 'Applying & verifying…' : 'Apply without starting'}</button>}><div className="configuration-state"><div><span>Default</span><b>{configurations.find((row) => row.is_default)?.name || '—'}</b></div><div><span>Selected</span><b>{configuration?.name || '—'} v{configuration?.version ?? '—'}</b><Badge tone="accent">SELECTED FOR NEXT RUN</Badge></div><div><span>Currently Applied</span><b>{applied ? `${applied.configuration_name} v${applied.configuration_version}` : 'Unknown'}</b><Badge tone={selectedApplied ? 'good' : 'muted'}>{selectedApplied ? 'CURRENT MATCH' : 'WILL APPLY ON START'}</Badge></div><div><span>Active Run Snapshot</span><b>{running ? context.configurationName : '—'}</b></div></div>{applied?.diff && Object.keys(applied.diff).length > 0 && <div className="diff-list"><h3>Requested vs Applied</h3>{Object.entries(applied.diff).map(([key, values]) => <div key={key}><span>{key}</span><code>Requested {String(values.requested)}</code><code>Applied {String(values.actual)}</code><Badge tone="warn">DIFFERENT</Badge></div>)}</div>}</Card>}
    {isRc && <Card title="RC Workflow State" sub="The complete Workflow is reapplied and gNB is restarted on every Start." right={<button className="btn" disabled={busy !== '' || running || configurationId == null} onClick={apply}>{busy === 'apply' ? 'Applying & verifying…' : 'Apply Workflow without starting'}</button>}><div className="configuration-state"><div><span>Workflow</span><b>RC Workflow</b></div><div><span>Currently Applied</span><b>{applied?.configuration_id === configurationId ? 'RC Workflow' : 'Unknown'}</b><Badge tone={selectedApplied ? 'good' : 'muted'}>{selectedApplied ? 'CURRENT MATCH' : 'WILL APPLY ON START'}</Badge></div><div><span>Active Run Snapshot</span><b>{running ? 'RC Workflow' : '—'}</b></div></div></Card>}

    <Card title="Preflight Checklist" sub={preflight?.ready ? 'Ready to Start · gNB restart and UE synchronization happen after Start' : `${preflight?.issues.length || 0} external dependency issue(s) need attention`}><div className="preflight-grid">{groups.map(([group, checks]) => <section key={group}><h3>{displayRunTerm(group)}</h3>{checks.map((check) => <div className={`preflight-row ${check.ok ? 'ok' : 'bad'}`} key={check.key}><span>{check.ok ? '✓' : '!'}</span><div><b>{displayRunTerm(check.label)}</b>{!check.ok && check.action && <button className="link-btn" onClick={() => check.action?.includes('Experiment') ? nav(`/experiments/${encodeURIComponent(experimentId)}`) : check.action?.includes('Data Import') ? nav('/sync') : nav('/advanced')}>{displayRunTerm(check.action)}</button>}</div></div>)}</section>)}</div></Card>

    <Card title="Run Summary" sub="Review the selected conditions before execution. Run ID is assigned only when Start is accepted." right={running ? <button className="btn danger" disabled={busy !== ''} onClick={stop}>{busy === 'stop' ? 'Stopping…' : 'Stop Run'}</button> : <button className="btn primary" disabled={busy !== '' || !preflight?.ready} onClick={start}>{busy === 'start' ? 'Restarting gNB…' : 'Start Run'}</button>}>{!preflight?.ready && <div className="run-blocked"><b>Run cannot start</b><span>{preflight?.issues.length || 0} issue(s) need attention:</span><ul>{preflight?.issues.map((issue) => <li key={issue.key}>{issue.label}</li>)}</ul></div>}<dl className="config-values"><div><dt>Experiment</dt><dd>{experimentId || '—'}</dd></div><div><dt>{isRc ? 'Workflow' : 'Configuration'}</dt><dd>{isRc ? 'RC Workflow' : `${configuration?.name || '—'} v${configuration?.version ?? '—'}`}</dd></div><div><dt>Environment</dt><dd>{experiment?.environment || '—'}</dd></div><div><dt>Execution Mode</dt><dd>{preflight?.execution_mode || '—'}</dd></div><div><dt>Run ID</dt><dd>Generated automatically on Start</dd></div><div><dt>Radio</dt><dd>{requested.frequencyMHz || '—'} MHz · {requested.bandwidthMHz || '—'} MHz BW</dd></div></dl></Card>

    {running && <Card title="Active Run Monitor" sub="Phase, gNB and measurement state are platform-authoritative." right={<button className="btn danger" disabled={busy !== ''} onClick={stop}>{busy === 'stop' ? 'Stopping…' : 'End task now'}</button>}><div className="configuration-state"><div><span>Run</span><b>{latestRun?.run_id}</b></div><div><span>gNB</span><Badge tone={runtime?.gnb_state === 'RUNNING' ? 'good' : 'warn'}>{runtime?.ac.state === 'restarting_gnb' ? 'RESTARTING' : runtime?.gnb_state || '—'}</Badge></div>{experiment?.environment === 'AC' ? <><div><span>Current Phase</span><b>{runtime?.ac.phase?.name?.toUpperCase() || (status.phone.status?.phase as string) || 'WAITING SYNC'}</b><small>{runtime?.ac.phase_index != null && runtime.ac.phase_index >= 0 ? `${runtime.ac.phase_index + 1} / ${runtime.ac.phase_count}` : 'Standard Run'}</small></div><div><span>LOADED Configuration</span><b>{runtime?.ac.phase?.name === 'loaded' ? runtime.ac.configuration_name || configuration?.name || '—' : '—'}</b></div></> : <><div><span>Sample / Angle</span><b>{runtime?.rc.current_sample_index ?? 0} / {runtime?.rc.n_steps ?? requested.rcChamber?.n_steps ?? '—'} · {runtime?.rc.current_angle_deg == null ? '—' : `${runtime.rc.current_angle_deg.toFixed(1)}°`}</b></div><div><span>Sample Phase</span><b>{runtime?.rc.state || 'Preparing'}</b></div><div><span>RSSP / Target</span><b>{runtime?.rc.last_rssp_db == null ? '—' : `${runtime.rc.last_rssp_db.toFixed(1)} / ${runtime.rc.target_rssp_db?.toFixed(1)} dBFS`}</b></div></>}</div>{campaign?.log?.length ? <div className="activity-list"><h3>Latest activity</h3>{campaign.log.slice(-3).reverse().map((item) => <div key={`${item.ms}-${item.msg}`}><time>{new Date(item.ms).toLocaleTimeString()}</time><span>{item.msg}</span></div>)}</div> : null}</Card>}
    <Card title="Live Throughput / RSSP / SNR" sub="Run-scoped 1 Hz gNB telemetry; hover to read the exact timestamp and values."><div style={{ height: 300 }}>{runtime?.series?.length ? <ResponsiveContainer><LineChart data={runtime.series.map((row) => ({ ...row, label: new Date(row.time).toLocaleTimeString() }))}><XAxis dataKey="label" minTickGap={28} /><YAxis yAxisId="rate" unit=" Mbps" /><YAxis yAxisId="radio" orientation="right" unit=" dB" /><Tooltip /><Legend /><Line yAxisId="rate" dataKey="throughput" name="UL throughput" stroke="#3157d5" dot={false} isAnimationActive={false} /><Line yAxisId="radio" dataKey="rssp" name="RSSP" stroke="#c0392b" dot={false} isAnimationActive={false} /><Line yAxisId="radio" dataKey="snr" name="SNR" stroke="#2e9e5b" dot={false} isAnimationActive={false} /></LineChart></ResponsiveContainer> : <div className="muted-text">Waiting for Run telemetry…</div>}</div></Card>
    {experiment?.environment === 'RC' && running && <Card title="Power Calibration Record" sub="Each point is one RSSP observation. Hover shows Sample, TX Gain, Target SNR and RSSP."><div style={{ height: 260 }}>{runtime?.rc.calibration_records?.length ? <ResponsiveContainer><LineChart data={runtime.rc.calibration_records}><XAxis dataKey="ms" tickFormatter={(value) => new Date(value).toLocaleTimeString()} /><YAxis unit=" dBFS" /><Tooltip labelFormatter={(value) => new Date(Number(value)).toLocaleTimeString()} formatter={(value, name, item) => name === 'RSSP' ? [`${Number(value).toFixed(2)} dBFS · TX Gain ${item.payload.tx_gain_db ?? '—'} dB · Target SNR ${item.payload.target_snr_db ?? '—'} dB`, `Sample ${item.payload.sample_index}`] : [value, name]} /><Line dataKey="rssp_db" name="RSSP" stroke="#3157d5" strokeWidth={2} dot={{ r: 4 }} isAnimationActive={false} /><ReferenceLine y={runtime.rc.target_rssp_db} stroke="#c0392b" strokeDasharray="6 4" label="Target RSSP" /></LineChart></ResponsiveContainer> : <div className="muted-text">Calibration points will appear when the first Sample reaches power calibration.</div>}</div></Card>}
  </div>;
}
