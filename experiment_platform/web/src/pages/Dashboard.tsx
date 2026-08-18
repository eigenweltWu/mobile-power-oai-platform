import { useCallback, useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Experiment, PlatformStatus } from '../types';
import { Badge, Card, ErrorBox, Spinner, StatCard, toast } from '../components/ui';
import { fmtBytes } from '../format';
import { useOperatorContext } from '../context';

type Configuration = { id: number; name: string; config_json: string; is_default?: number; version?: number };
type Check = { group: string; key: string; ok: boolean; label: string; action?: string | null };
type Applied = { configuration_id: number; configuration_name: string; configuration_version: number; status: string; actual_config?: Record<string, unknown>; diff?: Record<string, { requested: unknown; actual: unknown }> };
type Preflight = { ready: boolean; checks: Check[]; issues: Check[]; execution_mode: string; selected: { id: number; name: string; version: number; config: Record<string, any> }; applied: Applied | null };
type Campaign = { running: boolean; state: string; samples_done?: number; n_steps?: number; current_angle_deg?: number | null; last_rssp_db?: number | null; log?: { ms: number; stage: string; msg: string }[] };

const RUNNING = ['PREPARING', 'ARMED', 'RUNNING', 'STOPPING'];
const parse = (json: string) => { try { return JSON.parse(json) as Record<string, any>; } catch { return {}; } };

export default function Dashboard({ nav }: { nav: (path: string) => void }) {
  const { value: context, update: updateContext } = useOperatorContext();
  const [status, setStatus] = useState<PlatformStatus | null>(null);
  const [experiments, setExperiments] = useState<Experiment[]>([]);
  const [experimentId, setExperimentId] = useState(context.experimentId);
  const [configurations, setConfigurations] = useState<Configuration[]>([]);
  const [configurationId, setConfigurationId] = useState<number | null>(context.configurationId);
  const [preflight, setPreflight] = useState<Preflight | null>(null);
  const [campaign, setCampaign] = useState<Campaign | null>(null);
  const [busy, setBusy] = useState<'apply' | 'start' | 'stop' | ''>('');
  const [error, setError] = useState<Error | null>(null);

  const experiment = experiments.find((row) => row.experiment_id === experimentId);
  const configuration = configurations.find((row) => row.id === configurationId);
  const latestRun = status?.experiment.latest_run;
  const running = !!latestRun && RUNNING.includes(latestRun.state);

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
    api.get<Configuration[]>(`/api/experiments/${encodeURIComponent(experimentId)}/templates`).then((rows) => {
      setConfigurations(rows);
      setConfigurationId((old) => rows.some((row) => row.id === old) ? old : (rows.find((row) => row.is_default)?.id ?? rows[0]?.id ?? null));
    }).catch((cause) => setError(cause instanceof Error ? cause : new Error(String(cause))));
  }, [experimentId]);

  const loadPreflight = useCallback(async () => {
    if (!experimentId || configurationId == null) { setPreflight(null); return; }
    try { setPreflight(await api.get(`/api/run-control/preflight?experiment_id=${encodeURIComponent(experimentId)}&configuration_id=${configurationId}`)); }
    catch (cause) { setError(cause instanceof Error ? cause : new Error(String(cause))); }
  }, [experimentId, configurationId]);
  useEffect(() => { loadPreflight(); const timer = window.setInterval(loadPreflight, 4000); return () => window.clearInterval(timer); }, [loadPreflight]);

  useEffect(() => {
    if (experiment?.environment !== 'RC') { setCampaign(null); return; }
    const poll = () => api.get<Campaign>(`/api/rc/campaign/status?experiment_id=${encodeURIComponent(experimentId)}`).then(setCampaign).catch(() => undefined);
    poll(); const timer = window.setInterval(poll, 2000); return () => window.clearInterval(timer);
  }, [experimentId, experiment?.environment]);

  useEffect(() => updateContext({ experimentId, environment: experiment?.environment || '', configurationId,
    configurationName: configuration ? `${configuration.name} v${configuration.version ?? 1}` : '',
    runId: latestRun?.experiment_id === experimentId ? latestRun.run_id : '',
    status: latestRun?.experiment_id === experimentId ? latestRun.state : '',
  }), [experimentId, experiment?.environment, configurationId, configuration, latestRun, updateContext]);

  const apply = async () => {
    if (configurationId == null) return;
    setBusy('apply'); setError(null);
    try {
      const result = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/templates/${configurationId}/apply`, { serial: status?.phone.serial || '' });
      if (result.verification_status === 'VERIFIED') toast('ok', 'Configuration applied and verified.');
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

  return <div className="stack">
    <div className="page-head"><div><div className="title">Run Control</div><div className="subtitle">Select a Configuration, check external dependencies, then Start. Every Start applies it and force-restarts gNB.</div></div><button className="btn" onClick={() => { load(); loadPreflight(); }}>Refresh</button></div>
    {error && <ErrorBox error={error} />}
    <div className="stat-grid"><StatCard label="Phone" value={status.phone.state} sub={status.phone.device || 'Device unavailable'} tone={status.phone.state === 'CONNECTED' ? 'good' : 'bad'} /><StatCard label="gNB Current State" value={status.oai.gnb_running ? 'RUNNING' : 'STOPPED'} sub="Start always applies Selected and restarts gNB" tone={status.oai.gnb_running ? 'good' : 'muted'} /><StatCard label="UE Sync" value={status.oai.ue_in_sync ? 'CURRENTLY SYNCED' : 'WAITING'} sub="Established after Start and recorded as the Run time anchor" tone={status.oai.ue_in_sync ? 'good' : 'muted'} /><StatCard label="Storage" value={fmtBytes(status.storage.bytes)} sub={`${status.storage.n_files} indexed files`} tone="muted" /></div>

    <Card title="Run Context" sub="A fresh Run ID and standard timing values are generated automatically on every Start."><div className="grid cols-2"><label className="field"><span>Experiment</span><select value={experimentId} disabled={running} onChange={(event) => setExperimentId(event.target.value)}>{experiments.map((row) => <option key={row.experiment_id} value={row.experiment_id}>{row.experiment_id} · {row.environment}</option>)}</select></label><label className="field"><span>Selected for next Run</span><select value={configurationId ?? ''} disabled={running} onChange={(event) => setConfigurationId(Number(event.target.value))}>{configurations.map((row) => <option key={row.id} value={row.id}>{row.name} v{row.version ?? 1}{row.is_default ? ' · Default' : ''}</option>)}</select></label></div></Card>

    <Card title="Configuration State" sub="Applied is informational. Start always reapplies Selected and force-restarts gNB." right={<button className="btn" disabled={busy !== '' || running || configurationId == null} onClick={apply}>{busy === 'apply' ? 'Applying & verifying…' : 'Apply without starting'}</button>}><div className="configuration-state"><div><span>Default</span><b>{configurations.find((row) => row.is_default)?.name || '—'}</b></div><div><span>Selected</span><b>{configuration?.name || '—'} v{configuration?.version ?? '—'}</b><Badge tone="accent">SELECTED FOR NEXT RUN</Badge></div><div><span>Currently Applied</span><b>{applied ? `${applied.configuration_name} v${applied.configuration_version}` : 'Unknown'}</b><Badge tone={selectedApplied ? 'good' : 'muted'}>{selectedApplied ? 'CURRENT MATCH' : 'WILL APPLY ON START'}</Badge></div><div><span>Active Run Snapshot</span><b>{running ? context.configurationName : '—'}</b></div></div>{applied?.diff && Object.keys(applied.diff).length > 0 && <div className="diff-list"><h3>Requested vs Applied</h3>{Object.entries(applied.diff).map(([key, values]) => <div key={key}><span>{key}</span><code>Requested {String(values.requested)}</code><code>Applied {String(values.actual)}</code><Badge tone="warn">DIFFERENT</Badge></div>)}</div>}</Card>

    <Card title="Preflight Checklist" sub={preflight?.ready ? 'Ready to Start · gNB restart and UE synchronization happen after Start' : `${preflight?.issues.length || 0} external dependency issue(s) need attention`}><div className="preflight-grid">{groups.map(([group, checks]) => <section key={group}><h3>{group}</h3>{checks.map((check) => <div className={`preflight-row ${check.ok ? 'ok' : 'bad'}`} key={check.key}><span>{check.ok ? '✓' : '!'}</span><div><b>{check.label}</b>{!check.ok && check.action && <button className="link-btn" onClick={() => check.action?.includes('Experiment') ? nav(`/experiments/${encodeURIComponent(experimentId)}`) : check.action?.includes('Data Import') ? nav('/sync') : nav('/advanced')}>{check.action}</button>}</div></div>)}</section>)}</div></Card>

    <Card title="Run Summary" sub="Review the selected conditions before execution. Run ID is assigned only when Start is accepted." right={running ? <button className="btn danger" disabled={busy !== ''} onClick={stop}>{busy === 'stop' ? 'Stopping…' : 'Stop Run'}</button> : <button className="btn primary" disabled={busy !== '' || !preflight?.ready} onClick={start}>{busy === 'start' ? 'Restarting gNB…' : 'Start Run'}</button>}>{!preflight?.ready && <div className="run-blocked"><b>Run cannot start</b><span>{preflight?.issues.length || 0} issue(s) need attention:</span><ul>{preflight?.issues.map((issue) => <li key={issue.key}>{issue.label}</li>)}</ul></div>}<dl className="config-values"><div><dt>Experiment</dt><dd>{experimentId || '—'}</dd></div><div><dt>Configuration</dt><dd>{configuration?.name || '—'} v{configuration?.version ?? '—'}</dd></div><div><dt>Environment</dt><dd>{experiment?.environment || '—'}</dd></div><div><dt>Execution Mode</dt><dd>{preflight?.execution_mode || '—'}</dd></div><div><dt>Run ID</dt><dd>Generated automatically on Start</dd></div><div><dt>Radio</dt><dd>{requested.frequencyMHz || '—'} MHz · {requested.bandwidthMHz || '—'} MHz BW</dd></div></dl></Card>

    {running && <Card title="Active Run" sub="Stopping remains visible until backend confirmation."><div className="configuration-state"><div><span>Run</span><b>{latestRun?.run_id}</b></div><div><span>Status</span><Badge tone="warn">{busy === 'stop' ? 'STOPPING' : latestRun?.state}</Badge></div>{experiment?.environment === 'RC' && <><div><span>RC Stage</span><b>{campaign?.state || 'Preparing'}</b></div><div><span>Sample</span><b>{campaign?.samples_done ?? 0} / {campaign?.n_steps ?? requested.rcChamber?.n_steps ?? '—'}</b></div><div><span>Stirrer</span><b>{campaign?.current_angle_deg == null ? '—' : `${campaign.current_angle_deg.toFixed(1)}°`}</b></div><div><span>RSSP</span><b>{campaign?.last_rssp_db == null ? '—' : `${campaign.last_rssp_db.toFixed(1)} dBFS`}</b></div></>}</div>{campaign?.log?.length ? <div className="activity-list"><h3>Latest activity</h3>{campaign.log.slice(-3).reverse().map((item) => <div key={`${item.ms}-${item.msg}`}><time>{new Date(item.ms).toLocaleTimeString()}</time><span>{item.msg}</span></div>)}</div> : null}</Card>}
    <Card title="Live Throughput" sub="Live monitoring only · OAI NetworkTest is the shared traffic authority"><div className="muted-text">Session {status.oai.nettest?.state || 'IDLE'} · {status.oai.nettest?.initiator || '—'} · {status.oai.nettest?.direction?.toUpperCase() || '—'} {status.oai.nettest?.actualMbps?.toFixed(2) ?? '—'} Mbps</div><div className="muted-text">Telemetry UL {status.oai.throughput?.ulMbps?.toFixed(2) ?? '—'} Mbps · DL {status.oai.throughput?.dlMbps?.toFixed(2) ?? '—'} Mbps</div></Card>
  </div>;
}
