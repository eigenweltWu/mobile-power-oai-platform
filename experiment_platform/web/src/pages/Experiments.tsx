import { useEffect, useMemo, useState } from 'react';
import type { Dispatch, FormEvent, SetStateAction } from 'react';
import { api, downloadFile } from '../api';
import type { Experiment, Run } from '../types';
import { Badge, Card, EmptyState, ErrorBox, Field, Modal, Spinner, StaticValue, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { fmtIso, fmtTs } from '../format';

type ExperimentSummary = Experiment & {
  configuration_count?: number; run_count?: number; last_run_state?: string | null;
  last_quality_status?: string | null; last_activity_utc_ms?: number | null;
};
type Configuration = {
  id: number; experiment_id: string; name: string; config_json: string; created_utc: string;
  updated_utc?: string | null; is_default?: number; used_by_runs?: number;
};
type RunDetail = Run & {
  requested_config?: Record<string, unknown> | null; actual_config?: Record<string, unknown> | null;
  configuration_name?: string | null; last_error?: string | null;
  record_counts?: { phone: number; gnb: number; cir: number; clips: number };
};
type ConfigEditor = { id: number | null; name: string; config: Record<string, string>; original: string };
type DetailTab = 'overview' | 'configurations' | 'history';

const EMPTY_FORM = { experiment_id: '', environment: 'AC', operator_name: '', notes: '', purpose: '', flow: '' };
const PAGE_SIZE = 12;
const RUN_PAGE_SIZE = 15;
const DEFAULT_CONFIGURATION: Record<string, unknown> = {
  frequencyMHz: 3349.92, bandwidthMHz: 100, txGainDb: 60, rxGainDb: 40,
  puschTargetMode: 'manual', puschTargetSnrX10: 89, schedulerMode: 'auto', ulTrafficMbps: 5,
};
const CONFIG_KEYS = ['frequencyMHz', 'bandwidthMHz', 'txGainDb', 'rxGainDb', 'puschTargetMode', 'puschTargetSnrX10', 'schedulerMode', 'qm', 'mcs', 'nPrb', 'ulTrafficMbps'];
const QM_OPTIONS = [{ value: '2', label: '2 · QPSK' }, { value: '4', label: '4 · 16QAM' }, { value: '6', label: '6 · 64QAM' }, { value: '8', label: '8 · 256QAM' }];
const QM_MCS_RANGE: Record<string, { min: number; max: number }> = { '2': { min: 0, max: 9 }, '4': { min: 10, max: 16 }, '6': { min: 17, max: 27 }, '8': { min: 28, max: 31 } };

function parseConfig(json: string | null | undefined): Record<string, unknown> {
  try { return json ? JSON.parse(json) ?? {} : {}; } catch { return {}; }
}
function configForm(config: Record<string, unknown>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of CONFIG_KEYS) out[key] = config[key] == null ? '' : String(config[key]);
  if (!out.puschTargetMode) out.puschTargetMode = 'auto';
  if (!out.schedulerMode) out.schedulerMode = 'auto';
  if (!out.ulTrafficMbps) out.ulTrafficMbps = '5';
  return out;
}
function buildConfig(form: Record<string, string>): Record<string, unknown> {
  const number = (key: string) => Number(form[key]);
  const config: Record<string, unknown> = {
    frequencyMHz: number('frequencyMHz'), bandwidthMHz: number('bandwidthMHz'), txGainDb: number('txGainDb'), rxGainDb: number('rxGainDb'),
    puschTargetMode: form.puschTargetMode, schedulerMode: form.schedulerMode, ulTrafficMbps: number('ulTrafficMbps'),
  };
  if (form.puschTargetMode === 'manual') config.puschTargetSnrX10 = number('puschTargetSnrX10');
  if (form.schedulerMode === 'manual') Object.assign(config, { qm: number('qm'), mcs: number('mcs'), nPrb: number('nPrb') });
  return config;
}
function validateConfig(form: Record<string, string>): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const key of ['frequencyMHz', 'bandwidthMHz', 'txGainDb', 'rxGainDb', 'ulTrafficMbps']) if (form[key] === '' || !Number.isFinite(Number(form[key]))) errors[key] = 'Enter a valid number.';
  if (!errors.frequencyMHz && Number(form.frequencyMHz) <= 0) errors.frequencyMHz = 'Must be greater than 0 MHz.';
  if (!errors.bandwidthMHz && Number(form.bandwidthMHz) <= 0) errors.bandwidthMHz = 'Must be greater than 0 MHz.';
  for (const key of ['txGainDb', 'rxGainDb']) if (!errors[key] && (Number(form[key]) < 0 || Number(form[key]) > 100)) errors[key] = 'Expected range: 0–100 dB.';
  if (Number(form.ulTrafficMbps) < 0) errors.ulTrafficMbps = 'Must be 0 or greater.';
  if (form.puschTargetMode === 'manual') {
    const target = Number(form.puschTargetSnrX10);
    if (form.puschTargetSnrX10 === '' || !Number.isFinite(target)) errors.puschTargetSnrX10 = 'Required in manual mode.';
    else if (!Number.isInteger(target)) errors.puschTargetSnrX10 = 'Enter an integer in 0.1 dB units.';
  }
  if (form.schedulerMode === 'manual') {
    const range = QM_MCS_RANGE[form.qm]; const mcs = Number(form.mcs);
    if (!range) errors.qm = 'Select modulation.';
    if (form.mcs === '' || !Number.isFinite(mcs)) errors.mcs = 'Required.';
    else if (!Number.isInteger(mcs)) errors.mcs = 'Enter a whole MCS index.';
    else if (range && (mcs < range.min || mcs > range.max)) errors.mcs = `Valid range: ${range.min}–${range.max}.`;
    const nPrb = Number(form.nPrb);
    if (form.nPrb === '' || !Number.isFinite(nPrb)) errors.nPrb = 'Required.';
    else if (!Number.isInteger(nPrb) || nPrb < 1 || nPrb > 273) errors.nPrb = 'Enter a whole number from 1 to 273.';
  }
  return errors;
}
function summary(config: Record<string, unknown>): [string, string][] {
  const snr = Number(config.puschTargetSnrX10);
  const pusch = config.puschTargetMode === 'manual' && Number.isFinite(snr) ? `Manual · ${(snr / 10).toFixed(1)} dB` : 'Auto';
  const scheduler = config.schedulerMode === 'manual' ? `Manual · Qm ${config.qm ?? '—'} · MCS ${config.mcs ?? '—'}` : 'Auto';
  return [['Frequency', config.frequencyMHz == null ? '—' : `${config.frequencyMHz} MHz`], ['Bandwidth', config.bandwidthMHz == null ? '—' : `${config.bandwidthMHz} MHz`], ['TX / RX Gain', `${config.txGainDb ?? '—'} / ${config.rxGainDb ?? '—'} dB`], ['PUSCH', pusch], ['Scheduler', scheduler], ['UL Traffic', config.ulTrafficMbps == null ? '—' : `${config.ulTrafficMbps} Mbps`]];
}
function statusTone(status: string | null | undefined): 'good' | 'warn' | 'bad' | 'muted' {
  const value = status?.toUpperCase();
  if (value === 'PASS' || value === 'COMPLETE' || value === 'STOPPED') return 'good';
  if (value === 'WARNING' || value === 'RUNNING' || value === 'PREPARING') return 'warn';
  if (value === 'FAILED' || value === 'ERROR') return 'bad';
  return 'muted';
}
function ConfigValues({ data }: { data: Record<string, unknown> | null | undefined }) {
  if (!data) return <EmptyState>Configuration snapshot unavailable for this legacy Run.</EmptyState>;
  const entries = Object.entries(data);
  if (!entries.length) return <EmptyState>An empty Configuration was recorded.</EmptyState>;
  return <dl className="config-values">{entries.map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{typeof value === 'object' ? JSON.stringify(value) : String(value)}</dd></div>)}</dl>;
}

export default function Experiments({ nav, initialExperimentId = '' }: { nav: (path: string) => void; initialExperimentId?: string }) {
  const experiments = useLoad<ExperimentSummary[]>(() => api.get('/api/experiments'), []);
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [showCreate, setShowCreate] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<Error | null>(null);
  const [search, setSearch] = useState('');
  const [environment, setEnvironment] = useState('ALL');
  const [sort, setSort] = useState('newest');
  const [page, setPage] = useState(1);
  const selected = experiments.data?.find((x) => x.experiment_id === initialExperimentId) ?? null;

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    const rows = (experiments.data ?? []).filter((x) => (environment === 'ALL' || x.environment === environment) && (!q || `${x.experiment_id} ${x.operator_name ?? ''} ${x.purpose ?? ''}`.toLowerCase().includes(q)));
    rows.sort((a, b) => sort === 'oldest' ? String(a.created_utc).localeCompare(String(b.created_utc)) : sort === 'activity' ? (b.last_activity_utc_ms ?? 0) - (a.last_activity_utc_ms ?? 0) : String(b.created_utc).localeCompare(String(a.created_utc)));
    return rows;
  }, [experiments.data, search, environment, sort]);
  useEffect(() => setPage(1), [search, environment, sort]);

  const create = async (event: FormEvent) => {
    event.preventDefault(); if (!form.experiment_id.trim()) return;
    setCreating(true); setCreateError(null);
    try {
      const created = await api.post<Experiment & { configuration_error?: string }>('/api/experiments', { ...form, experiment_id: form.experiment_id.trim() });
      setShowCreate(false); setForm({ ...EMPTY_FORM }); experiments.reload();
      if (created.configuration_error) toast('err', `Experiment created, but Default Configuration failed: ${created.configuration_error}`);
      else toast('ok', 'Experiment and Default Configuration created.');
      nav(`/experiments/${encodeURIComponent(created.experiment_id)}`);
    } catch (error) { setCreateError(error instanceof Error ? error : new Error(String(error))); }
    finally { setCreating(false); }
  };
  const exportExperiment = async (experimentId: string) => {
    try { await downloadFile(`/api/experiments/${encodeURIComponent(experimentId)}/export`, `${experimentId}.zip`); toast('ok', `Export started for ${experimentId}.`); }
    catch (error) { toast('err', error instanceof Error ? error.message : String(error)); }
  };
  const deleteExperiment = async (experimentId: string) => {
    const typed = window.prompt(`This permanently deletes conditions, Runs, Configurations and derived files.\n\nType ${experimentId} to confirm.`);
    if (typed !== experimentId) return;
    try { await api.delete(`/api/experiments/${encodeURIComponent(experimentId)}`); toast('ok', `Experiment ${experimentId} deleted.`); experiments.reload(); nav('/experiments'); }
    catch (error) { toast('err', error instanceof Error ? error.message : String(error)); }
  };

  if (initialExperimentId) {
    if (experiments.loading && !experiments.data) return <Spinner label="Loading Experiment…" />;
    if (experiments.error) return <ErrorBox error={experiments.error} />;
    if (!selected) return <Card title="Experiment not found"><button className="btn" onClick={() => nav('/experiments')}>← Experiments</button></Card>;
    return <ExperimentDetail experiment={selected} nav={nav} onReload={experiments.reload} onExport={() => exportExperiment(selected.experiment_id)} onDelete={() => deleteExperiment(selected.experiment_id)} />;
  }

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));
  const shown = filtered.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE);
  return <div className="stack">
    <div className="page-head"><div><div className="title">Experiments</div><div className="subtitle">Configure here. Review here. Run from Dashboard.</div></div><button className="btn primary" onClick={() => setShowCreate(true)}>+ New Experiment</button></div>
    <div className="experiment-toolbar">
      <input aria-label="Search experiments" placeholder="Search ID, operator or purpose" value={search} onChange={(e) => setSearch(e.target.value)} />
      <select aria-label="Environment filter" value={environment} onChange={(e) => setEnvironment(e.target.value)}><option value="ALL">All environments</option><option value="AC">AC</option><option value="RC">RC</option></select>
      <select aria-label="Sort experiments" value={sort} onChange={(e) => setSort(e.target.value)}><option value="newest">Newest created</option><option value="oldest">Oldest created</option><option value="activity">Last activity</option></select>
      <button className="btn" onClick={experiments.reload}>Refresh</button>
    </div>
    {experiments.loading && !experiments.data ? <Spinner /> : experiments.error ? <ErrorBox error={experiments.error} /> : !experiments.data?.length ? <EmptyState><b>No experiments</b><br />Create your first Experiment.</EmptyState> : !filtered.length ? <EmptyState>No Experiments match the current search and filter.</EmptyState> : <>
      <div className="grid cols-2">{shown.map((item) => {
        const result = item.last_quality_status || item.last_run_state;
        return <article key={item.experiment_id} className={`card experiment-card ${item.environment === 'RC' ? 'rc' : 'ac'}`}>
          <div className="row between"><div><h2 className="mono">{item.experiment_id}</h2><div className="card-sub">Created {fmtIso(item.created_utc)}</div></div><Badge tone={item.environment === 'RC' ? 'warn' : 'accent'}>{item.environment ?? '—'}</Badge></div>
          <div className="experiment-purpose">{item.purpose || 'No purpose recorded.'}</div><dl className="kv"><dt>Operator</dt><dd>{item.operator_name || '—'}</dd></dl>
          <div className="experiment-card-stats"><span><b>{item.configuration_count ?? 0}</b> Configurations</span><span><b>{item.run_count ?? 0}</b> Runs / History</span><span>Last result {result ? <Badge tone={statusTone(result)}>{result}</Badge> : '—'}</span><span>Last activity <b>{fmtTs(item.last_activity_utc_ms)}</b></span></div>
          <div className="row between" style={{ marginTop: 16 }}><button className="btn primary" onClick={() => nav(`/experiments/${encodeURIComponent(item.experiment_id)}`)}>Manage Experiment</button><details className="more-menu"><summary aria-label={`More actions for ${item.experiment_id}`}>•••</summary><div><button onClick={() => exportExperiment(item.experiment_id)}>Export experiment</button><button className="danger-text" onClick={() => deleteExperiment(item.experiment_id)}>Delete experiment</button></div></details></div>
        </article>;
      })}</div>
      <div className="pagination"><span>{filtered.length} Experiments · page {page} of {totalPages}</span><button className="btn sm" disabled={page === 1} onClick={() => setPage((x) => x - 1)}>Previous</button><button className="btn sm" disabled={page === totalPages} onClick={() => setPage((x) => x + 1)}>Next</button></div>
    </>}
    {showCreate && <Modal title="New Experiment" sub="Create the Experiment first, then manage its Configurations." onClose={() => setShowCreate(false)} footer={<><button className="btn" onClick={() => setShowCreate(false)}>Cancel</button><button className="btn primary" form="create-experiment" type="submit" disabled={creating}>{creating ? 'Creating…' : 'Create & Configure'}</button></>}>
      <form id="create-experiment" className="stack" onSubmit={create}>{createError && <ErrorBox error={createError} />}<div className="notice-box">A Default Configuration will be created automatically and shown explicitly after creation.</div><div className="grid cols-2"><Field label="Experiment ID"><input required autoFocus className="mono" value={form.experiment_id} onChange={(e) => setForm({ ...form, experiment_id: e.target.value })} /></Field><Field label="Environment"><select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}><option value="AC">AC</option><option value="RC">RC</option></select></Field><Field label="Operator"><input value={form.operator_name} onChange={(e) => setForm({ ...form, operator_name: e.target.value })} /></Field></div><Field label="Purpose"><textarea rows={2} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} /></Field><Field label="Flow"><textarea rows={2} value={form.flow} onChange={(e) => setForm({ ...form, flow: e.target.value })} /></Field><Field label="Notes"><textarea rows={2} value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} /></Field></form>
    </Modal>}
  </div>;
}

function ExperimentDetail({ experiment, nav, onReload, onExport, onDelete }: {
  experiment: ExperimentSummary; nav: (path: string) => void; onReload: () => void; onExport: () => void; onDelete: () => void;
}) {
  const configurations = useLoad<Configuration[]>(() => api.get(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/templates`), [experiment.experiment_id]);
  const runs = useLoad<Run[]>(() => api.get(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/runs`), [experiment.experiment_id]);
  const [tab, setTab] = useState<DetailTab>('overview');
  const [overview, setOverview] = useState({ operator_name: experiment.operator_name || '', purpose: experiment.purpose || '', flow: experiment.flow || '', notes: experiment.notes || '' });
  const [overviewBaseline, setOverviewBaseline] = useState('');
  const [overviewError, setOverviewError] = useState<Error | null>(null);
  const [savingOverview, setSavingOverview] = useState(false);
  const [editor, setEditor] = useState<ConfigEditor | null>(null);
  const [configErrors, setConfigErrors] = useState<Record<string, string>>({});
  const [configApiError, setConfigApiError] = useState<Error | null>(null);
  const [savingConfig, setSavingConfig] = useState(false);
  const [runPage, setRunPage] = useState(1);
  const [selectedRunId, setSelectedRunId] = useState('');

  useEffect(() => {
    const next = { operator_name: experiment.operator_name || '', purpose: experiment.purpose || '', flow: experiment.flow || '', notes: experiment.notes || '' };
    setOverview(next); setOverviewBaseline(JSON.stringify(next));
  }, [experiment]);
  const overviewDirty = overviewBaseline !== '' && JSON.stringify(overview) !== overviewBaseline;
  const configDirty = !!editor && JSON.stringify({ name: editor.name, config: editor.config }) !== editor.original;
  const dirty = overviewDirty || configDirty;
  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => { if (dirty) event.preventDefault(); };
    window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn);
  }, [dirty]);
  const allowLeave = () => !dirty || window.confirm('Discard unsaved changes?');
  const changeTab = (next: DetailTab) => { if (next === tab || allowLeave()) { setTab(next); setEditor(null); } };
  const back = () => { if (allowLeave()) nav('/experiments'); };

  const saveOverview = async () => {
    setSavingOverview(true); setOverviewError(null);
    try { await api.put(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}`, overview); setOverviewBaseline(JSON.stringify(overview)); onReload(); toast('ok', 'Experiment details saved.'); }
    catch (error) { setOverviewError(error instanceof Error ? error : new Error(String(error))); }
    finally { setSavingOverview(false); }
  };
  const openConfiguration = (row?: Configuration, duplicate = false) => {
    if (configDirty && !window.confirm('Discard unsaved changes?')) return;
    const config = configForm(row ? parseConfig(row.config_json) : DEFAULT_CONFIGURATION);
    const next = { id: duplicate ? null : row?.id ?? null, name: row ? `${row.name}${duplicate ? ' Copy' : ''}` : '', config };
    setEditor({ ...next, original: JSON.stringify({ name: next.name, config: next.config }) }); setConfigErrors({}); setConfigApiError(null);
  };
  const saveConfiguration = async () => {
    if (!editor) return;
    const errors = validateConfig(editor.config); if (!editor.name.trim()) errors.name = 'Configuration name is required.';
    setConfigErrors(errors); if (Object.keys(errors).length) return;
    setSavingConfig(true); setConfigApiError(null);
    try {
      const body = { name: editor.name.trim(), config: buildConfig(editor.config) };
      if (editor.id == null) await api.post(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/templates`, body);
      else await api.put(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/templates/${editor.id}`, body);
      setEditor(null); configurations.reload(); onReload(); toast('ok', 'Configuration saved.');
    } catch (error) { setConfigApiError(error instanceof Error ? error : new Error(String(error))); }
    finally { setSavingConfig(false); }
  };
  const setDefault = async (row: Configuration) => {
    try { await api.post(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/templates/${row.id}/default`); configurations.reload(); onReload(); toast('ok', `${row.name} is now the Default Configuration.`); }
    catch (error) { setConfigApiError(error instanceof Error ? error : new Error(String(error))); }
  };
  const archive = async (row: Configuration) => {
    if (!window.confirm(`Archive Configuration “${row.name}”? Historical Run snapshots will remain unchanged.`)) return;
    try { await api.delete(`/api/experiments/${encodeURIComponent(experiment.experiment_id)}/templates/${row.id}`); configurations.reload(); onReload(); toast('ok', 'Configuration archived.'); }
    catch (error) { setConfigApiError(error instanceof Error ? error : new Error(String(error))); }
  };
  const removeRun = async (runId: string) => {
    if (!window.confirm(`Delete Run ${runId} and its indexed records?`)) return;
    try { await api.delete(`/api/runs/${encodeURIComponent(runId)}`); runs.reload(); setSelectedRunId(''); toast('ok', 'Run deleted.'); }
    catch (error) { toast('err', error instanceof Error ? error.message : String(error)); }
  };
  const defaultConfiguration = configurations.data?.find((x) => !!x.is_default);
  const totalRunPages = Math.max(1, Math.ceil((runs.data?.length ?? 0) / RUN_PAGE_SIZE));
  const visibleRuns = (runs.data ?? []).slice((runPage - 1) * RUN_PAGE_SIZE, runPage * RUN_PAGE_SIZE);

  return <div className="stack experiment-detail">
    <div className="page-head"><div><button className="btn ghost" onClick={back}>← Experiments</button><div className="title mono">{experiment.experiment_id}</div><div className="subtitle">Experiment management · execution controls are on Dashboard</div></div><Badge tone={experiment.environment === 'RC' ? 'warn' : 'accent'}>{experiment.environment ?? '—'}</Badge></div>
    <div className="tabs" role="tablist">{(['overview', 'configurations', 'history'] as DetailTab[]).map((item) => <button key={item} className={tab === item ? 'active' : ''} onClick={() => changeTab(item)}>{item === 'overview' ? 'Overview' : item === 'configurations' ? `Configurations (${configurations.data?.length ?? 0})` : `History (${runs.data?.length ?? 0})`}</button>)}</div>

    {tab === 'overview' && <Card title="Overview" sub="Experiment metadata. ID, environment and creation time are read only."><div className="stack">
      {overviewError && <ErrorBox error={overviewError} />}<div className="grid cols-3"><Field label="Experiment ID"><StaticValue>{experiment.experiment_id}</StaticValue></Field><Field label="Environment"><StaticValue>{experiment.environment ?? '—'}</StaticValue></Field><Field label="Created time"><StaticValue>{fmtIso(experiment.created_utc)}</StaticValue></Field></div>
      <Field label="Operator"><input value={overview.operator_name} onChange={(e) => setOverview({ ...overview, operator_name: e.target.value })} /></Field><Field label="Purpose"><textarea rows={3} value={overview.purpose} onChange={(e) => setOverview({ ...overview, purpose: e.target.value })} /></Field><Field label="Flow"><textarea rows={3} value={overview.flow} onChange={(e) => setOverview({ ...overview, flow: e.target.value })} /></Field><Field label="Notes"><textarea rows={3} value={overview.notes} onChange={(e) => setOverview({ ...overview, notes: e.target.value })} /></Field>
      <div><button className="btn primary" disabled={!overviewDirty || savingOverview} onClick={saveOverview}>{savingOverview ? 'Saving…' : 'Save changes'}</button></div>
    </div></Card>}

    {tab === 'configurations' && <div className="stack">
      {configApiError && <ErrorBox error={configApiError} />}
      {experiment.environment === 'RC' && <Card title="RC Configuration" sub="Mechanical sampling and stirrer settings are part of this RC Experiment."><button className="btn" onClick={() => { if (allowLeave()) nav(`/experiments/${encodeURIComponent(experiment.experiment_id)}/rc`); }}>Open RC Setup</button></Card>}
      <Card title="Default Configuration" sub="Used for the next Run unless Dashboard explicitly selects another Configuration.">
        {configurations.loading && !configurations.data ? <Spinner /> : configurations.error ? <><ErrorBox error={configurations.error} /><button className="btn sm" onClick={configurations.reload}>Retry</button></> : defaultConfiguration ? <ConfigurationCard row={defaultConfiguration} onEdit={() => openConfiguration(defaultConfiguration)} onDuplicate={() => openConfiguration(defaultConfiguration, true)} /> : <EmptyState><b>Configuration required</b><br />Add a Configuration, then explicitly set it as default.</EmptyState>}
      </Card>
      <Card title="Saved Configurations" sub="Cards are read-only until an explicit action is selected." right={<button className="btn" onClick={() => openConfiguration()}>+ Add Configuration</button>}>
        {configurations.loading && !configurations.data ? <Spinner /> : configurations.error ? <><ErrorBox error={configurations.error} /><button className="btn sm" onClick={configurations.reload}>Retry</button></> : !configurations.data?.length ? <EmptyState><b>No configurations</b><br />Add a Configuration before running this Experiment.</EmptyState> : <div className="stack">{configurations.data.map((row) => <ConfigurationCard key={row.id} row={row} onEdit={() => openConfiguration(row)} onDuplicate={() => openConfiguration(row, true)} onDefault={row.is_default ? undefined : () => setDefault(row)} onArchive={row.is_default ? undefined : () => archive(row)} />)}</div>}
      </Card>
      {editor && <ConfigurationEditor editor={editor} setEditor={setEditor} errors={configErrors} apiError={configApiError} saving={savingConfig} onSave={saveConfiguration} onCancel={() => { if (!configDirty || window.confirm('Discard unsaved changes?')) setEditor(null); }} />}
    </div>}

    {tab === 'history' && <div className="stack"><Card title="Run History" sub="Newest first. Each Run uses its execution-time Configuration snapshot.">
      {runs.loading && !runs.data ? <Spinner /> : runs.error ? <><ErrorBox error={runs.error} /><button className="btn sm" onClick={runs.reload}>Retry</button></> : !runs.data?.length ? <EmptyState><b>No run history</b><br />No Runs have been recorded yet. Execute Runs from Dashboard.</EmptyState> : <><div className="table-wrap"><table className="data"><thead><tr><th>Time</th><th>Run ID</th><th>Configuration</th><th>Result</th><th>Quality</th><th>Actions</th></tr></thead><tbody>{visibleRuns.map((run) => <tr key={run.run_id}><td>{fmtTs(run.started_utc_ms)}</td><td className="mono">{run.run_id}</td><td>{run.configuration_name || (run.requested_config_json ? 'Recorded snapshot' : <span className="muted-text">Snapshot unavailable</span>)}</td><td><Badge tone={statusTone(run.state)}>{run.state}</Badge></td><td>{run.quality_status ? <Badge tone={statusTone(run.quality_status)}>{run.quality_status}</Badge> : '—'}</td><td><button className="btn sm" onClick={() => setSelectedRunId(run.run_id)}>View result</button></td></tr>)}</tbody></table></div><div className="pagination"><span>Page {runPage} of {totalRunPages}</span><button className="btn sm" disabled={runPage === 1} onClick={() => setRunPage((x) => x - 1)}>Previous</button><button className="btn sm" disabled={runPage === totalRunPages} onClick={() => setRunPage((x) => x + 1)}>Next</button></div></>}
    </Card>{selectedRunId && <RunResultDetail runId={selectedRunId} nav={nav} onDelete={() => removeRun(selectedRunId)} onClose={() => setSelectedRunId('')} />}</div>}

    <details className="danger-zone"><summary>Danger Zone</summary><div className="row between"><div><b>Export or delete this Experiment</b><div className="muted-text">Deletion includes conditions, Runs, Configurations and derived files.</div></div><div className="row"><button className="btn" onClick={onExport}>Export everything</button><button className="btn danger" onClick={onDelete}>Delete Experiment</button></div></div></details>
  </div>;
}

function ConfigurationCard({ row, onEdit, onDuplicate, onDefault, onArchive }: {
  row: Configuration; onEdit: () => void; onDuplicate: () => void; onDefault?: () => void; onArchive?: () => void;
}) {
  const config = parseConfig(row.config_json);
  return <article className={`configuration-card ${row.is_default ? 'default' : ''}`}>
    <div className="row between"><div><b>{row.name}</b><div className="muted-text">Created {fmtIso(row.created_utc)} · used by {row.used_by_runs ?? 0} Run(s)</div></div>{row.is_default ? <Badge tone="accent">DEFAULT FOR NEXT RUN</Badge> : null}</div>
    <dl className="configuration-summary">{summary(config).map(([label, value]) => <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>
    <details className="details"><summary>View full configuration</summary><div className="details-body"><ConfigValues data={config} /></div></details>
    <div className="row gap-sm" style={{ marginTop: 10 }}><button className="btn sm" onClick={onEdit}>Edit</button><button className="btn sm" onClick={onDuplicate}>Duplicate</button>{onDefault && <button className="btn sm" onClick={onDefault}>Set as default</button>}{onArchive && <button className="btn sm ghost" onClick={onArchive}>Archive</button>}</div>
  </article>;
}

function ConfigurationEditor({ editor, setEditor, errors, apiError, saving, onSave, onCancel }: {
  editor: ConfigEditor; setEditor: Dispatch<SetStateAction<ConfigEditor | null>>; errors: Record<string, string>;
  apiError: Error | null; saving: boolean; onSave: () => void; onCancel: () => void;
}) {
  const c = editor.config;
  const set = (key: string, value: string) => setEditor((old) => old ? { ...old, config: { ...old.config, [key]: value } } : old);
  const input = (label: string, key: string, hint?: string) => <Field label={label} hint={hint}><input className="mono" type="number" value={c[key]} onChange={(e) => set(key, e.target.value)} />{errors[key] && <span className="field-error">{errors[key]}</span>}</Field>;
  const range = QM_MCS_RANGE[c.qm];
  return <Card title={editor.id == null ? 'Add Configuration' : 'Edit Configuration'} sub="Unsaved changes are protected when you leave this editor."><div className="stack">
    {apiError && <ErrorBox error={apiError} />}<Field label="Configuration name"><input value={editor.name} onChange={(e) => setEditor({ ...editor, name: e.target.value })} />{errors.name && <span className="field-error">{errors.name}</span>}</Field>
    <section><h3>RF</h3><div className="grid cols-4">{input('Frequency', 'frequencyMHz', 'MHz')}{input('Bandwidth', 'bandwidthMHz', 'MHz')}{input('TX Gain', 'txGainDb', 'dB')}{input('RX Gain', 'rxGainDb', 'dB')}</div></section>
    <section><h3>PUSCH</h3><div className="grid cols-3"><Field label="Mode"><select value={c.puschTargetMode} onChange={(e) => set('puschTargetMode', e.target.value)}><option value="auto">Auto</option><option value="manual">Manual</option></select></Field>{c.puschTargetMode === 'manual' && input('Target SNR', 'puschTargetSnrX10', `${c.puschTargetSnrX10 || '89'} → ${(Number(c.puschTargetSnrX10 || 89) / 10).toFixed(1)} dB`)}</div></section>
    <section><h3>UL Scheduler</h3><div className="grid cols-4"><Field label="Mode"><select value={c.schedulerMode} onChange={(e) => set('schedulerMode', e.target.value)}><option value="auto">Auto</option><option value="manual">Manual</option></select></Field>{c.schedulerMode === 'manual' && <><Field label="Modulation"><select value={c.qm} onChange={(e) => set('qm', e.target.value)}><option value="">Select…</option>{QM_OPTIONS.map((x) => <option key={x.value} value={x.value}>{x.label}</option>)}</select>{errors.qm && <span className="field-error">{errors.qm}</span>}</Field>{input('MCS', 'mcs', range ? `${range.min}–${range.max} for selected modulation` : 'Select modulation first')}{input('N_PRB', 'nPrb')}</>}</div></section>
    <section><h3>Traffic</h3><div className="grid cols-3">{input('UL Traffic', 'ulTrafficMbps', 'Mbps · values ≥100 request saturation')}</div></section>
    <div className="row"><button className="btn primary" disabled={saving} onClick={onSave}>{saving ? 'Saving…' : 'Save Configuration'}</button><button className="btn" onClick={onCancel}>Cancel</button></div>
  </div></Card>;
}

function RunResultDetail({ runId, nav, onDelete, onClose }: { runId: string; nav: (path: string) => void; onDelete: () => void; onClose: () => void }) {
  const run = useLoad<RunDetail>(() => api.get(`/api/runs/${encodeURIComponent(runId)}`), [runId]);
  if (run.loading && !run.data) return <Card title="Run Result"><Spinner /></Card>;
  if (run.error) return <Card title="Run Result"><ErrorBox error={run.error} /><button className="btn sm" onClick={run.reload}>Retry</button></Card>;
  if (!run.data) return null;
  const item = run.data;
  return <Card title="Result Detail" sub={`${item.experiment_id} / ${item.run_id}`} right={<button className="btn sm" onClick={onClose}>Close</button>}><div className="stack">
    <div className="result-context"><div><span>Experiment</span><b>{item.experiment_id}</b></div><div><span>Run</span><b>{item.run_id}</b></div><div><span>Configuration</span><b>{item.configuration_name || (item.requested_config ? 'Recorded snapshot' : 'Snapshot unavailable')}</b></div><div><span>Status</span><Badge tone={statusTone(item.quality_status || item.state)}>{item.quality_status || item.state}</Badge></div></div>
    <section><h3>Run Summary</h3><dl className="config-values"><div><dt>Run ID</dt><dd>{item.run_id}</dd></div><div><dt>Experiment ID</dt><dd>{item.experiment_id}</dd></div><div><dt>Condition ID</dt><dd>{item.condition_id}</dd></div><div><dt>Started</dt><dd>{fmtTs(item.started_utc_ms)}</dd></div><div><dt>Completed</dt><dd>{fmtTs(item.ended_utc_ms)}</dd></div><div><dt>State</dt><dd>{item.state}</dd></div><div><dt>Quality</dt><dd>{item.quality_status || 'Not evaluated'}</dd></div></dl></section>
    <section><h3>Requested Configuration</h3><ConfigValues data={item.requested_config} /></section><section><h3>Applied / Verified Configuration</h3><ConfigValues data={item.actual_config} /></section>
    <section><h3>Result</h3><div className="result-counts"><span>Phone records <b>{item.record_counts?.phone ?? 0}</b></span><span>gNB records <b>{item.record_counts?.gnb ?? 0}</b></span><span>CIR records <b>{item.record_counts?.cir ?? 0}</b></span><span>Clips <b>{item.record_counts?.clips ?? 0}</b></span></div>{item.last_error && <ErrorBox error={item.last_error} />}<button className="btn primary" onClick={() => nav(`/timeline/${encodeURIComponent(item.experiment_id)}/${encodeURIComponent(item.run_id)}`)}>Open Records & Clips</button></section>
    <details className="danger-zone"><summary>Run actions</summary><div className="row between"><span className="muted-text">Deleting removes this Run and its indexed records.</span><button className="btn danger" onClick={onDelete}>Delete Run</button></div></details>
  </div></Card>;
}
