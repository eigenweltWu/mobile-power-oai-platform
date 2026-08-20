import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Experiment, PlatformStatus } from '../types';
import { readAcExecution } from '../acConfiguration';
import { DEFAULT_GNB_CONFIGURATION, GnbConfigurationFields, TABLE2_MCS_RANGE, type GnbConfigurationKey, type GnbConfigurationValue } from '../components/GnbConfigurationFields';
import { Badge, Card, ErrorBox, Field, Modal, Spinner, StatCard, toast } from '../components/ui';
import { fmtBytes } from '../format';
import { useOperatorContext } from '../context';
import { Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

type Configuration = { id: number; name: string; config_json: string; is_default?: number; version?: number };
type Check = { group: string; key: string; ok: boolean; label: string; action?: string | null };
type Applied = { configuration_id: number; configuration_name: string; configuration_version: number; status: string; actual_config?: Record<string, unknown>; diff?: Record<string, { requested: unknown; actual: unknown }> };
type Preflight = { ready: boolean; checks: Check[]; issues: Check[]; execution_mode: string; selected: { id: number; name: string; version: number; config: Record<string, any> }; applied: Applied | null };
type Campaign = { running: boolean; state: string; samples_done?: number; n_steps?: number; current_angle_deg?: number | null; last_rssp_db?: number | null; intervention_required?: boolean; intervention_reason?: string | null; log?: { ms: number; stage: string; msg: string }[] };
type Runtime = {
  gnb_state: string; environment: string; run?: { run_id: string; state: string } | null;
  ac: { state: string; phase_index?: number; phase_count?: number; phase?: { name: string; durationSeconds: number; configurationId?: number }; phase_remaining_s?: number | null; configuration_name?: string };
  rc: Campaign & { current_sample_index?: number; target_rssp_db?: number; current_tx_gain_db?: number; pusch_x10?: number; calibration_records?: Array<{ ms: number; sample_index: number; rssp_db: number; target_rssp_db: number; tx_gain_db?: number; rx_gain_db?: number; target_snr_db?: number; calibration_actuator?: string; settle_sample_count?: number }> };
  series: Array<{ time: number; throughput?: number | null; rssp?: number | null; snr?: number | null }>;
};

const RUNNING = ['PREPARING', 'ARMED', 'RUNNING', 'STOPPING'];
const parse = (json: string) => { try { return JSON.parse(json) as Record<string, any>; } catch { return {}; } };
const runtimeForm = (config: Record<string, any>): GnbConfigurationValue => ({
  frequencyMHz: config.frequencyMHz ?? DEFAULT_GNB_CONFIGURATION.frequencyMHz,
  bandwidthMHz: config.bandwidthMHz ?? DEFAULT_GNB_CONFIGURATION.bandwidthMHz,
  txGainDb: config.txGainDb ?? DEFAULT_GNB_CONFIGURATION.txGainDb,
  rxGainDb: config.rxGainDb ?? DEFAULT_GNB_CONFIGURATION.rxGainDb,
  puschTargetSnrDb: Number(config.puschTargetSnrX10 ?? DEFAULT_GNB_CONFIGURATION.puschTargetSnrDb * 10) / 10,
  schedulerMode: config.schedulerMode === 'manual' ? 'manual' : 'auto',
  qm: config.qm ?? DEFAULT_GNB_CONFIGURATION.qm, mcs: config.mcs ?? DEFAULT_GNB_CONFIGURATION.mcs,
  nPrb: config.nPrb ?? DEFAULT_GNB_CONFIGURATION.nPrb, ulTrafficMbps: config.ulTrafficMbps ?? 5,
});
const runtimeConfig = (form: GnbConfigurationValue, base: Record<string, any>) => {
  const config: Record<string, any> = { ...base, frequencyMHz: Number(form.frequencyMHz), bandwidthMHz: Number(form.bandwidthMHz), txGainDb: Number(form.txGainDb), rxGainDb: Number(form.rxGainDb), puschTargetMode: 'manual', puschTargetSnrX10: Math.round(Number(form.puschTargetSnrDb) * 10), schedulerMode: form.schedulerMode, ulTrafficMbps: Number(form.ulTrafficMbps) };
  if (form.schedulerMode === 'manual') Object.assign(config, { qm: Number(form.qm), mcs: Number(form.mcs), nPrb: Number(form.nPrb) });
  else { delete config.qm; delete config.mcs; delete config.nPrb; }
  return config;
};

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
  const [busy, setBusy] = useState<'start' | 'stop' | ''>('');
  const [view, setView] = useState<'setup' | 'readiness' | 'monitor'>('setup');
  const [error, setError] = useState<Error | null>(null);
  const [runForm, setRunForm] = useState<GnbConfigurationValue>({ ...DEFAULT_GNB_CONFIGURATION, ulTrafficMbps: 5 });
  const [runFormBaseline, setRunFormBaseline] = useState('');
  const [showRunEditor, setShowRunEditor] = useState(false);

  const experiment = experiments.find((row) => row.experiment_id === experimentId);
  const configuration = configurations.find((row) => row.id === configurationId);
  const latestRun = status?.experiment.latest_run;
  const running = !!latestRun && RUNNING.includes(latestRun.state);
  const isRc = experiment?.environment === 'RC';
  const usesAcTemplate = experiment?.environment === 'AC' && !!experiment.ac_template_enabled;
  const acExecution = readAcExecution(experiment?.ac_template_json);

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
      setConfigurationId(rows.find((row) => row.is_default)?.id ?? rows[0]?.id ?? null);
    }).catch((cause) => { if (!cancelled) setError(cause instanceof Error ? cause : new Error(String(cause))); });
    return () => { cancelled = true; };
  }, [experimentId]);

  useEffect(() => {
    if (!configuration) return;
    const base = parse(configuration.config_json); const next = runtimeForm(base);
    setRunForm(next); setRunFormBaseline(JSON.stringify(runtimeConfig(next, base)));
  }, [configuration?.id, configuration?.config_json]);
  useEffect(() => {
    if (!configurations.length) return;
    const firstTemplateId = usesAcTemplate ? readAcExecution(experiment?.ac_template_json).rows[0]?.configurationId : undefined;
    const desired = configurations.find((row) => row.id === firstTemplateId)?.id ?? configurations.find((row) => row.is_default)?.id ?? configurations[0].id;
    setConfigurationId((current) => current === desired ? current : desired);
  }, [configurations, experiment?.ac_template_json, usesAcTemplate]);

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

  const runFormDirty = !!configuration && runFormBaseline !== '' && JSON.stringify(runtimeConfig(runForm, parse(configuration.config_json))) !== runFormBaseline;
  const schedulerRange = TABLE2_MCS_RANGE[Number(runForm.qm)];
  const runFormValid = ['frequencyMHz', 'bandwidthMHz', 'txGainDb', 'rxGainDb', 'puschTargetSnrDb', 'ulTrafficMbps'].every((key) => Number.isFinite(Number(runForm[key as GnbConfigurationKey]))) && Number(runForm.frequencyMHz) > 0 && Number(runForm.bandwidthMHz) > 0 && (runForm.schedulerMode !== 'manual' || !!schedulerRange && Number(runForm.mcs) >= schedulerRange[0] && Number(runForm.mcs) <= schedulerRange[1] && Number(runForm.nPrb) >= 1);

  const start = async (restart = false) => {
    if (configurationId == null || !configuration || (!usesAcTemplate && !isRc && !runFormValid)) return;
    setBusy('start'); setError(null);
    try {
      if (restart && running) await api.post(`/api/experiments/${encodeURIComponent(latestRun?.experiment_id || experimentId)}/stop`, { serial: status?.phone.serial || '' });
      let startConfigurationId = configurationId;
      if (!isRc && !usesAcTemplate && runFormDirty) {
        const updated = await api.put<Configuration>(`/api/experiments/${encodeURIComponent(experimentId)}/templates/${configurationId}`, { name: configuration.name, config: runtimeConfig(runForm, parse(configuration.config_json)) });
        startConfigurationId = updated.id; setConfigurationId(updated.id); setRunFormBaseline(JSON.stringify(runtimeConfig(runForm, parse(configuration.config_json))));
        const rows = await api.get<Configuration[]>(`/api/experiments/${encodeURIComponent(experimentId)}/templates`); setConfigurations(rows);
      }
      const nextPreflight = await api.get<Preflight>(`/api/run-control/preflight?experiment_id=${encodeURIComponent(experimentId)}&configuration_id=${startConfigurationId}`);
      if (!nextPreflight.ready) throw new Error(nextPreflight.issues.map((issue) => issue.label).join('; ') || 'Run is not ready.');
      setPreflight(nextPreflight);
      const result = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/start`, { template_id: startConfigurationId, serial: status?.phone.serial || '', ...(!isRc && !usesAcTemplate ? { idle_seconds: acExecution.warmupSeconds, collection_seconds: acExecution.testSeconds } : {}) });
      updateContext({ runId: result.run_id, status: 'PREPARING' });
      setShowRunEditor(false);
      toast('ok', result.rc_campaign_started ? 'Run started; RC is waiting for post-restart UE sync.' : 'gNB restarted; sync → warm-up → test started.');
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
  const startDisabled = busy !== '' || !preflight?.ready || (usesAcTemplate && !acExecution.rows.length) || (!isRc && !usesAcTemplate && !runFormValid);
  const runButtons = <div className="row">{running && !isRc && !usesAcTemplate && <button className="btn primary" disabled={startDisabled} onClick={() => start(true)}>{busy === 'start' ? 'Restarting…' : 'Restart with Parameters'}</button>}{!running && <button className="btn primary" disabled={startDisabled} onClick={() => start()}>{busy === 'start' ? 'Restarting gNB…' : 'Start Run'}</button>}{running && <button className="btn danger" disabled={busy !== ''} onClick={stop}>{busy === 'stop' ? 'Stopping…' : 'Stop Run'}</button>}</div>;
  const effectiveRequested = !isRc && !usesAcTemplate && configuration ? runtimeConfig(runForm, parse(configuration.config_json)) : requested;

  return <div className="stack page-workspace run-control">
    <div className="page-head"><div><div className="title">Run Control</div><div className="subtitle">{isRc ? 'Review the complete RC Workflow, check external dependencies, then Start.' : usesAcTemplate ? 'Run the saved Template sequence from top to bottom.' : 'Edit the Default Configuration at any time; every Start or Restart performs sync → warm-up → test.'}</div></div><div className="row"><button className="btn" onClick={() => { load(); loadPreflight(); }}>Refresh</button>{runButtons}</div></div>
    {error && <ErrorBox error={error} />}
    <div className="tabs workspace-tabs" role="tablist" aria-label="Run control stages"><button className={view === 'setup' ? 'active' : ''} onClick={() => setView('setup')}>1 · Setup</button><button className={view === 'readiness' ? 'active' : ''} onClick={() => setView('readiness')}>2 · Readiness <Badge tone={preflight?.ready ? 'good' : 'warn'}>{preflight?.ready ? 'READY' : `${preflight?.issues.length || 0} ISSUES`}</Badge></button><button className={view === 'monitor' ? 'active' : ''} onClick={() => setView('monitor')}>3 · Live Monitor {running && <Badge tone="good">LIVE</Badge>}</button></div>
    <div className="workspace-content stack">
    {view === 'setup' && <>
    <div className="stat-grid"><StatCard label="Phone" value={status.phone.state} sub={status.phone.device || 'Device unavailable'} tone={status.phone.state === 'CONNECTED' ? 'good' : 'bad'} /><StatCard label="gNB Current State" value={status.oai.gnb_running ? 'RUNNING' : 'STOPPED'} sub={isRc ? 'Start always reapplies the Workflow and restarts gNB' : 'Start always applies Selected and restarts gNB'} tone={status.oai.gnb_running ? 'good' : 'muted'} /><StatCard label="UE Sync" value={status.oai.ue_in_sync ? 'CURRENTLY SYNCED' : 'WAITING'} sub="Established after Start and recorded as the Run time anchor" tone={status.oai.ue_in_sync ? 'good' : 'muted'} /><StatCard label="Storage" value={fmtBytes(status.storage.bytes)} sub={`${status.storage.n_files} indexed files`} tone="muted" /></div>

    <Card title="Run Context" sub="A fresh Run ID is generated on every Start."><div className="grid cols-2"><label className="field"><span>Experiment</span><select value={experimentId} disabled={running} onChange={(event) => setExperimentId(event.target.value)}>{experiments.map((row) => <option key={row.experiment_id} value={row.experiment_id}>{row.experiment_id} · {row.environment}</option>)}</select></label>{!isRc && <div className="field"><span>Default Configuration</span><b>{configuration?.name || '—'} v{configuration?.version ?? '—'}</b></div>}{isRc && <div className="field"><span>Workflow</span><b>RC Workflow</b></div>}</div></Card>

    {!isRc && !usesAcTemplate && <Card title="Run Parameters" sub="Changes are saved to the Default Configuration when Start or Restart is pressed." right={<button className="btn" onClick={() => setShowRunEditor(true)}>Edit Parameters</button>}><dl className="configuration-summary"><div><dt>Radio</dt><dd>{runForm.frequencyMHz} MHz · {runForm.bandwidthMHz} MHz BW</dd></div><div><dt>TX / RX Gain</dt><dd>{runForm.txGainDb} / {runForm.rxGainDb} dB</dd></div><div><dt>PUSCH Target</dt><dd>{runForm.puschTargetSnrDb} dB</dd></div><div><dt>Scheduler</dt><dd>{runForm.schedulerMode === 'manual' ? `Qm ${runForm.qm} · MCS ${runForm.mcs}` : 'Auto'}</dd></div><div><dt>Warm-up / Test</dt><dd>{acExecution.warmupSeconds} / {acExecution.testSeconds} s</dd></div><div><dt>Draft</dt><dd>{runFormDirty ? <Badge tone="warn">UNSAVED</Badge> : <Badge tone="good">DEFAULT</Badge>}</dd></div></dl></Card>}
    {!isRc && usesAcTemplate && <Card title="Phase Sequence" sub="Each Phase performs synchronization, warm-up and test with its saved Configuration."><dl className="configuration-summary"><div><dt>Phases</dt><dd>{acExecution.rows.length}</dd></div><div><dt>Initial Configuration</dt><dd>{configuration?.name || '—'}</dd></div><div><dt>Execution</dt><dd>Automatic completion</dd></div></dl>{!acExecution.rows.length && <div className="error-box">Add and save at least one Phase before starting.</div>}</Card>}
    {isRc && <Card title="RC Workflow State" sub="Start applies and verifies the complete Workflow, restarts gNB, and then begins the Run."><div className="configuration-state"><div><span>Workflow</span><b>RC Workflow</b></div><div><span>Currently Applied</span><b>{applied?.configuration_id === configurationId ? 'RC Workflow' : 'Unknown'}</b><Badge tone={selectedApplied ? 'good' : 'muted'}>{selectedApplied ? 'CURRENT MATCH' : 'WILL APPLY ON START'}</Badge></div><div><span>Active Run Snapshot</span><b>{running ? 'RC Workflow' : '—'}</b></div></div></Card>}
    </>}

    {view === 'readiness' && <>
    <Card title="Preflight Checklist" sub={preflight?.ready ? 'Ready to Start · gNB restart and UE synchronization happen after Start' : `${preflight?.issues.length || 0} external dependency issue(s) need attention`}><div className="preflight-grid">{groups.map(([group, checks]) => <section key={group}><h3>{displayRunTerm(group)}</h3>{checks.map((check) => <div className={`preflight-row ${check.ok ? 'ok' : 'bad'}`} key={check.key}><span>{check.ok ? '✓' : '!'}</span><div><b>{displayRunTerm(check.label)}</b>{!check.ok && check.action && <button className="link-btn" onClick={() => check.action?.includes('Experiment') ? nav(`/experiments/${encodeURIComponent(experimentId)}`) : check.action?.includes('Data Import') ? nav('/sync') : nav('/advanced')}>{displayRunTerm(check.action)}</button>}</div></div>)}</section>)}</div></Card>

    <Card title="Run Summary" sub="Review the conditions before execution. Run ID is assigned only when Start is accepted." right={runButtons}>{!preflight?.ready && <div className="run-blocked"><b>Run cannot start</b><span>{preflight?.issues.length || 0} issue(s) need attention:</span><ul>{preflight?.issues.map((issue) => <li key={issue.key}>{issue.label}</li>)}</ul></div>}<dl className="config-values"><div><dt>Experiment</dt><dd>{experimentId || '—'}</dd></div><div><dt>{isRc ? 'Workflow' : 'Configuration'}</dt><dd>{isRc ? 'RC Workflow' : `${configuration?.name || '—'} v${configuration?.version ?? '—'}`}</dd></div><div><dt>Environment</dt><dd>{experiment?.environment || '—'}</dd></div><div><dt>Execution Mode</dt><dd>{preflight?.execution_mode || '—'}</dd></div><div><dt>Run ID</dt><dd>Generated automatically on Start</dd></div><div><dt>Radio</dt><dd>{effectiveRequested.frequencyMHz || '—'} MHz · {effectiveRequested.bandwidthMHz || '—'} MHz BW</dd></div></dl></Card>
    </>}

    {view === 'monitor' && <>
    {isRc && running && campaign?.intervention_required && <RcIntervention experimentId={experimentId} reason={campaign.intervention_reason || 'Calibration requires user configuration'} />}

    {running && <Card title="Active Run Monitor" sub="Phase, gNB and measurement state are platform-authoritative." right={<button className="btn danger" disabled={busy !== ''} onClick={stop}>{busy === 'stop' ? 'Stopping…' : 'End task now'}</button>}><div className="configuration-state"><div><span>Run</span><b>{latestRun?.run_id}</b></div><div><span>gNB</span><Badge tone={runtime?.gnb_state === 'RUNNING' ? 'good' : 'warn'}>{runtime?.ac.state === 'restarting_gnb' ? 'RESTARTING' : runtime?.gnb_state || '—'}</Badge></div>{experiment?.environment === 'AC' ? <><div><span>Current Phase</span><b>{runtime?.ac.phase?.name?.toUpperCase() || (status.phone.status?.phase as string) || 'WAITING SYNC'}</b><small>{runtime?.ac.phase_index != null && runtime.ac.phase_index >= 0 ? `${runtime.ac.phase_index + 1} / ${runtime.ac.phase_count}` : 'Standard Run'}</small></div><div><span>LOADED Configuration</span><b>{runtime?.ac.phase?.name === 'loaded' ? runtime.ac.configuration_name || configuration?.name || '—' : '—'}</b></div></> : <><div><span>Sample / Angle</span><b>{runtime?.rc.current_sample_index ?? 0} / {runtime?.rc.n_steps ?? requested.rcChamber?.n_steps ?? '—'} · {runtime?.rc.current_angle_deg == null ? '—' : `${runtime.rc.current_angle_deg.toFixed(1)}°`}</b></div><div><span>Sample Phase</span><b>{runtime?.rc.state || 'Preparing'}</b></div><div><span>RSSP / Target</span><b>{runtime?.rc.last_rssp_db == null ? '—' : `${runtime.rc.last_rssp_db.toFixed(1)} / ${runtime.rc.target_rssp_db?.toFixed(1)} dBFS`}</b></div></>}</div>{campaign?.log?.length ? <div className="activity-list"><h3>Latest activity</h3>{campaign.log.slice(-3).reverse().map((item) => <div key={`${item.ms}-${item.msg}`}><time>{new Date(item.ms).toLocaleTimeString()}</time><span>{item.msg}</span></div>)}</div> : null}</Card>}
    <Card title="Live Throughput / RSSP / SNR" sub="Run-scoped 1 Hz gNB telemetry; hover to read the exact timestamp and values."><div style={{ height: 300 }}>{runtime?.series?.length ? <ResponsiveContainer><LineChart data={runtime.series.map((row) => ({ ...row, label: new Date(row.time).toLocaleTimeString() }))}><XAxis dataKey="label" minTickGap={28} /><YAxis yAxisId="rate" unit=" Mbps" /><YAxis yAxisId="radio" orientation="right" unit=" dB" /><Tooltip /><Legend /><Line yAxisId="rate" dataKey="throughput" name="UL throughput" stroke="#3157d5" dot={false} isAnimationActive={false} /><Line yAxisId="radio" dataKey="rssp" name="RSSP" stroke="#c0392b" dot={false} isAnimationActive={false} /><Line yAxisId="radio" dataKey="snr" name="SNR" stroke="#2e9e5b" dot={false} isAnimationActive={false} /></LineChart></ResponsiveContainer> : <div className="muted-text">Waiting for Run telemetry…</div>}</div></Card>
    {experiment?.environment === 'RC' && running && <Card title="Power Calibration Record" sub="Each point is the RSSP mean measured during Settle Time after signal appears."><div style={{ height: 260 }}>{runtime?.rc.calibration_records?.length ? <ResponsiveContainer><LineChart data={runtime.rc.calibration_records}><XAxis dataKey="ms" tickFormatter={(value) => new Date(value).toLocaleTimeString()} /><YAxis unit=" dBFS" /><Tooltip labelFormatter={(value) => new Date(Number(value)).toLocaleTimeString()} formatter={(value, name, item) => name === 'RSSP' ? [`${Number(value).toFixed(2)} dBFS · ${item.payload.settle_sample_count ?? 0} samples · RX Gain ${item.payload.rx_gain_db ?? '—'} dB · Target SNR ${item.payload.target_snr_db ?? '—'} dB`, `Sample ${item.payload.sample_index} · ${item.payload.calibration_actuator ?? '—'}`] : [value, name]} /><Line dataKey="rssp_db" name="RSSP" stroke="#3157d5" strokeWidth={2} dot={{ r: 4 }} isAnimationActive={false} /><ReferenceLine y={runtime.rc.target_rssp_db} stroke="#c0392b" strokeDasharray="6 4" label="Target RSSP" /></LineChart></ResponsiveContainer> : <div className="muted-text">Calibration points will appear when the first Sample reaches power calibration.</div>}</div></Card>}
    </>}
    </div>
    {showRunEditor && <Modal size="lg" title="Default Run Parameters" sub="The draft is applied to the Default Configuration only when Start or Restart is pressed." onClose={() => setShowRunEditor(false)} footer={<><button className="btn" onClick={() => setShowRunEditor(false)}>Done</button>{running && <button className="btn primary" disabled={startDisabled} onClick={() => start(true)}>{busy === 'start' ? 'Restarting…' : 'Restart with These Parameters'}</button>}</>}><GnbConfigurationFields value={runForm} onChange={(key, value) => setRunForm((current) => ({ ...current, [key]: value }))} includeTraffic /></Modal>}
  </div>;
}

type InterventionForm = {
  frequencyMHz: number; bandwidthMHz: number; txGainDb: number; rxGainDb: number;
  puschTargetSnrDb: number; schedulerMode: 'auto' | 'manual'; qm: number; mcs: number; nPrb: number;
};
const TABLE2_RANGE: Record<number, [number, number]> = { 2: [0, 4], 4: [5, 10], 6: [11, 19], 8: [20, 27] };

function RcIntervention({ experimentId, reason }: { experimentId: string; reason: string }) {
  const [form, setForm] = useState<InterventionForm | null>(null);
  const [bandwidths, setBandwidths] = useState<number[]>([20, 40, 100]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>();
  useEffect(() => { api.get<{ configuration: Record<string, unknown>; supportedBandwidthMHz?: number[] }>('/api/diagnostics/rssp-calibration/gnb').then((result) => {
    const c = result.configuration;
    const qm = TABLE2_RANGE[Number(c.qm)] ? Number(c.qm) : 8;
    const [mcsLow, mcsHigh] = TABLE2_RANGE[qm];
    setBandwidths(result.supportedBandwidthMHz || [20, 40, 100]);
    setForm({ frequencyMHz: Number(c.frequencyMHz), bandwidthMHz: Number(c.bandwidthMHz), txGainDb: Number(c.txGainDb), rxGainDb: Number(c.rxGainDb), puschTargetSnrDb: Number(c.puschTargetSnrX10 ?? 100) / 10, schedulerMode: c.schedulerMode === 'manual' ? 'manual' : 'auto', qm, mcs: Math.min(mcsHigh, Math.max(mcsLow, Number(c.mcs ?? mcsHigh))), nPrb: Number(c.nPrb ?? 50) });
  }).catch(setError); }, []);
  if (!form) return <Card title="RC paused for user configuration" sub={reason}>{error ? <ErrorBox error={error} /> : <Spinner />}</Card>;
  const [mcsMin, mcsMax] = TABLE2_RANGE[form.qm] || TABLE2_RANGE[8];
  const numberField = (label: string, key: 'frequencyMHz' | 'txGainDb' | 'rxGainDb' | 'nPrb', unit?: string) => <Field label={label} hint={unit}><input type="number" step={key === 'txGainDb' || key === 'rxGainDb' ? 0.01 : undefined} className="mono" disabled={busy} value={form[key]} onChange={(event) => setForm({ ...form, [key]: Number(event.target.value) })} /></Field>;
  const applyAndContinue = async () => {
    setBusy(true); setError(undefined);
    try {
      await api.post('/api/diagnostics/rssp-calibration/gnb/configure', { frequencyMHz: form.frequencyMHz, bandwidthMHz: form.bandwidthMHz, txGainDb: form.txGainDb, rxGainDb: form.rxGainDb, puschTargetMode: 'manual', puschTargetSnrX10: Math.round(form.puschTargetSnrDb * 10), schedulerMode: form.schedulerMode, ...(form.schedulerMode === 'manual' ? { qm: form.qm, mcs: form.mcs, nPrb: form.nPrb } : {}) });
      await api.post('/api/rc/campaign/resume', { experimentId });
      toast('ok', 'gNB configuration verified; continuing with the next RC sample.');
    } catch (cause) { setError(cause); }
    finally { setBusy(false); }
  };
  return <Card title="RC paused for user configuration" sub="The failed sample will be skipped. Apply a verified gNB restart, then the campaign continues with the next sample."><div className="stack"><div className="notice-box"><b>{reason.replace(/_/g, ' ')}</b></div>{error ? <ErrorBox error={error} /> : null}<section><h3>RF</h3><div className="grid cols-4">{numberField('Frequency', 'frequencyMHz', 'MHz')}<Field label="Bandwidth" hint="MHz"><select disabled={busy} value={form.bandwidthMHz} onChange={(event) => setForm({ ...form, bandwidthMHz: Number(event.target.value) })}>{bandwidths.map((value) => <option key={value} value={value}>{value} MHz</option>)}</select></Field>{numberField('TX Gain', 'txGainDb', 'dB')}{numberField('RX Gain', 'rxGainDb', 'dB')}</div></section><section><h3>PUSCH Target · Manual</h3><Field label="Target SNR" hint="Calculation 0.01 dB · OAI applies 0.1 dB"><div className="row"><input className="grow" type="range" min={0} max={40} step={0.01} disabled={busy} value={form.puschTargetSnrDb} onChange={(event) => setForm({ ...form, puschTargetSnrDb: Number(event.target.value) })} /><b className="mono">{form.puschTargetSnrDb.toFixed(2)} dB</b></div></Field></section><section><h3>UL Scheduler</h3><div className="grid cols-4"><Field label="Mode"><select disabled={busy} value={form.schedulerMode} onChange={(event) => setForm({ ...form, schedulerMode: event.target.value as 'auto' | 'manual' })}><option value="auto">Auto</option><option value="manual">Manual</option></select></Field>{form.schedulerMode === 'manual' && <><Field label="Qm" hint="MCS Table 2 only"><select disabled={busy} value={form.qm} onChange={(event) => { const qm = Number(event.target.value); const [low, high] = TABLE2_RANGE[qm]; setForm({ ...form, qm, mcs: Math.min(high, Math.max(low, form.mcs)) }); }}>{[2, 4, 6, 8].map((value) => <option key={value} value={value}>Qm {value}</option>)}</select></Field><Field label="MCS" hint={`Table 2 · ${mcsMin}–${mcsMax}`}><div className="row"><input className="grow" type="range" min={mcsMin} max={mcsMax} step={1} disabled={busy} value={form.mcs} onChange={(event) => setForm({ ...form, mcs: Number(event.target.value) })} /><b>{form.mcs}</b></div></Field>{numberField('N_PRB', 'nPrb')}</>}</div></section><button className="btn primary" disabled={busy} onClick={applyAndContinue}>{busy ? 'Applying, restarting and verifying…' : 'Apply configuration, restart gNB and continue next sample'}</button></div></Card>;
}
