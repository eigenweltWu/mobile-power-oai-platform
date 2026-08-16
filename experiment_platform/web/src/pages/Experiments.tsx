import { useEffect, useState } from 'react';
import { api, downloadFile } from '../api';
import type { Experiment, PlatformStatus } from '../types';
import { Badge, Card, EmptyState, ErrorBox, Field, FullScreenLoader, Modal, Spinner, StaticValue, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { fmtIso } from '../format';

type Template = { id: number; experiment_id: string; name: string; config_json: string; created_utc: string };
type Run = { run_id: string; state: string; experiment_id: string; condition_id: string; quality_status: string | null };

/** Phone adb serial is fixed by the rig; users must not edit it. */
const PHONE_SERIAL = '53616213';

const EMPTY_FORM = { experiment_id: '', environment: 'AC', operator_name: '', notes: '', purpose: '', flow: '' };

/** Complete set of OAI configurable properties (backend apply_condition keys). */
type OaiFieldDef = { key: string; label: string; type: 'number' | 'select'; options?: string[] };

const OAI_FIELDS: OaiFieldDef[] = [
  { key: 'frequencyMHz', label: 'Frequency (MHz)', type: 'number' },
  { key: 'bandwidthMHz', label: 'Bandwidth (MHz)', type: 'number' },
  { key: 'txGainDb', label: 'TX gain (dB)', type: 'number' },
  { key: 'rxGainDb', label: 'RX gain (dB)', type: 'number' },
  { key: 'puschTargetMode', label: 'PUSCH target mode', type: 'select', options: ['auto', 'manual'] },
  { key: 'puschTargetSnrX10', label: 'PUSCH target SNR (×10)', type: 'number' },
  { key: 'schedulerMode', label: 'Scheduler mode', type: 'select', options: ['auto', 'manual'] },
  { key: 'mcs', label: 'MCS', type: 'number' },
  { key: 'qm', label: 'Qm', type: 'number' },
  { key: 'nPrb', label: 'N_PRB', type: 'number' },
];

function emptyOaiConfig(): Record<string, string> {
  const c: Record<string, string> = {};
  for (const f of OAI_FIELDS) c[f.key] = '';
  return c;
}

function configJsonToForm(configJson: string): Record<string, string> {
  const form = emptyOaiConfig();
  let obj: Record<string, unknown> = {};
  try {
    obj = JSON.parse(configJson) ?? {};
  } catch {
    /* invalid JSON -> all fields empty */
  }
  for (const f of OAI_FIELDS) {
    const v = obj[f.key];
    if (v !== undefined && v !== null) form[f.key] = String(v);
  }
  // ensure manual-mode sub-fields always have concrete values (no blanks)
  if (form.puschTargetMode === 'manual' && !form.puschTargetSnrX10) form.puschTargetSnrX10 = '89';
  if (form.schedulerMode === 'manual') {
    if (!form.qm) form.qm = '2';
    if (!form.mcs) form.mcs = '0';
    if (!form.nPrb) form.nPrb = '273';
  }
  return form;
}

function formToConfigObject(form: Record<string, string>): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  for (const f of OAI_FIELDS) {
    const v = form[f.key];
    if (v === undefined || v === '') continue;
    if (f.type === 'number') {
      const n = Number(v);
      cfg[f.key] = Number.isFinite(n) ? n : v;
    } else {
      cfg[f.key] = v;
    }
  }
  return cfg;
}

function configSummary(configJson: string): { key: string; label: string; value: string }[] {
  const form = configJsonToForm(configJson);
  const out: { key: string; label: string; value: string }[] = [];
  for (const f of OAI_FIELDS) {
    if (form[f.key] !== '') out.push({ key: f.key, label: f.label, value: form[f.key] });
  }
  return out;
}

/** Config every experiment gets by default (the "Default" template) — fully explicit. */
const DEFAULT_TEMPLATE_CONFIG: Record<string, unknown> = {
  frequencyMHz: 3349.92,
  bandwidthMHz: 100,
  txGainDb: 60,
  rxGainDb: 40,
  puschTargetMode: 'manual',
  puschTargetSnrX10: 89,
  schedulerMode: 'auto',
};

/** Qm → (modulation, feasible MCS range) for the UL scheduler. */
const QM_OPTIONS = [
  { value: '2', label: '2 · QPSK' },
  { value: '4', label: '4 · 16QAM' },
  { value: '6', label: '6 · 64QAM' },
  { value: '8', label: '8 · 256QAM' },
];
const QM_MCS_RANGE: Record<string, { min: number; max: number }> = {
  '2': { min: 0, max: 9 },
  '4': { min: 10, max: 16 },
  '6': { min: 17, max: 27 },
  '8': { min: 28, max: 31 },
};
function mcsRangeHint(qm: string): string {
  const r = QM_MCS_RANGE[qm];
  return r ? `MCS ${r.min}–${r.max}` : 'select Qm';
}

/** Compare a template's config_json with the experiment's initial_oai_config. */
function configsEqual(a: string | null | undefined, b: string): boolean {
  if (!a) return false;
  let oa: unknown = a;
  let ob: unknown = b;
  try {
    oa = JSON.parse(a);
  } catch {
    /* raw string */
  }
  try {
    ob = JSON.parse(b);
  } catch {
    /* raw string */
  }
  return JSON.stringify(oa) === JSON.stringify(ob);
}

/** Build the config object from the form, honouring auto/manual modes. */
function buildTemplateConfig(c: Record<string, string>): Record<string, unknown> {
  const num = (k: string): unknown => {
    const v = c[k];
    if (v === undefined || v === '') return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : v;
  };
  const cfg: Record<string, unknown> = {
    frequencyMHz: num('frequencyMHz'),
    bandwidthMHz: num('bandwidthMHz'),
    txGainDb: num('txGainDb'),
    rxGainDb: num('rxGainDb'),
    puschTargetMode: c.puschTargetMode || 'auto',
    schedulerMode: c.schedulerMode || 'auto',
  };
  if (cfg.puschTargetMode === 'manual') {
    const snr = num('puschTargetSnrX10');
    if (snr !== undefined) cfg.puschTargetSnrX10 = snr;
  }
  if (cfg.schedulerMode === 'manual') {
    const qm = num('qm');
    const mcs = num('mcs');
    const nPrb = num('nPrb');
    if (qm !== undefined) cfg.qm = qm;
    if (mcs !== undefined) cfg.mcs = mcs;
    if (nPrb !== undefined) cfg.nPrb = nPrb;
  }
  return cfg;
}

/** Return an error string when the form has a blank/invalid field, else null. */
function validateTemplateConfig(c: Record<string, string>): string | null {
  for (const k of ['frequencyMHz', 'bandwidthMHz', 'txGainDb', 'rxGainDb']) {
    const v = c[k];
    if (v === undefined || v === '' || !Number.isFinite(Number(v))) return `${k} must be a number`;
  }
  if (c.puschTargetMode === 'manual') {
    const v = c.puschTargetSnrX10;
    if (v === undefined || v === '' || !Number.isFinite(Number(v))) return 'PUSCH target SNR (×10) is required in manual mode';
  }
  if (c.schedulerMode === 'manual') {
    const qm = c.qm;
    if (!qm) return 'select Qm (modulation)';
    const mcs = Number(c.mcs);
    if (c.mcs === undefined || c.mcs === '' || !Number.isFinite(mcs)) return 'MCS is required';
    const r = QM_MCS_RANGE[qm];
    if (r && (mcs < r.min || mcs > r.max)) return `MCS ${mcs} is out of range ${r.min}–${r.max} for Qm ${qm}`;
    const nPrb = Number(c.nPrb);
    if (c.nPrb === undefined || c.nPrb === '' || !Number.isFinite(nPrb)) return 'N_PRB is required';
  }
  return null;
}

/** Map an OAI template config onto a condition payload for the auto-run flow. */
function conditionFromTemplate(conditionId: string, exp: Experiment, cfg: Record<string, unknown>): Record<string, unknown> {
  const payload: Record<string, unknown> = {
    condition_id: conditionId,
    experiment_id: exp.experiment_id,
    environment: exp.environment,
  };
  const map: [string, string][] = [
    ['frequencyMHz', 'frequency_mhz'],
    ['bandwidthMHz', 'bandwidth_mhz'],
    ['txGainDb', 'tx_gain_db'],
    ['rxGainDb', 'rx_gain_db'],
    ['puschTargetMode', 'pusch_target_mode'],
    ['puschTargetSnrX10', 'pusch_target_snr_x10'],
    ['schedulerMode', 'scheduler_mode'],
    ['mcs', 'mcs'],
    ['qm', 'qm'],
    ['nPrb', 'n_prb'],
  ];
  for (const [k, field] of map) {
    if (cfg[k] !== undefined && cfg[k] !== null) payload[field] = cfg[k];
  }
  return payload;
}

export default function Experiments({ nav }: { nav: (p: string) => void }) {
  const { data, error, loading, reload } = useLoad<Experiment[]>(() => api.get('/api/experiments'), []);
  const [phoneState, setPhoneState] = useState<string>('OFFLINE');
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);

  // task editor state (floating window)
  const [selected, setSelected] = useState<Experiment | null>(null);
  const [editorLoading, setEditorLoading] = useState(false);
  const [edit, setEdit] = useState({ purpose: '', flow: '', notes: '' });
  const [templates, setTemplates] = useState<Template[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [tplModal, setTplModal] = useState<{ name: string; config: Record<string, string> } | null>(null);
  const [tplEditing, setTplEditing] = useState<number | null>(null);

  // full-screen operation overlay + delete confirm floating window
  const [overlay, setOverlay] = useState<{ title: string; steps: string[]; active: number } | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<Experiment | null>(null);
  const [deleting, setDeleting] = useState(false);

  /** Refresh the phone connection state (triggered before every operation). */
  const detectPhone = async (): Promise<string> => {
    try {
      const s = await api.get<PlatformStatus>('/api/platform/status');
      const st = s.phone.state?.toUpperCase() ?? 'OFFLINE';
      setPhoneState(st);
      return st;
    } catch {
      setPhoneState('OFFLINE');
      return 'OFFLINE';
    }
  };

  useEffect(() => {
    detectPhone();
  }, []);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!form.experiment_id.trim()) return;
    setCreating(true);
    const expId = form.experiment_id.trim();
    try {
      await api.post('/api/experiments', {
        experiment_id: expId,
        environment: form.environment,
        operator_name: form.operator_name,
        notes: form.notes,
        purpose: form.purpose,
        flow: form.flow,
      });
      // seed the default template and use it as the startup config
      try {
        await api.post(`/api/experiments/${encodeURIComponent(expId)}/templates`, { name: 'Default', config: DEFAULT_TEMPLATE_CONFIG });
        await api.put(`/api/experiments/${encodeURIComponent(expId)}`, { initial_oai_config: DEFAULT_TEMPLATE_CONFIG });
      } catch {
        /* the editor also guarantees a Default template */
      }
      setForm({ ...EMPTY_FORM });
      setShowCreate(false);
      toast('ok', `experiment ${expId} created`);
      reload();
    } catch (err) {
      toast('err', err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  /** Load templates (guaranteeing a Default template) + runs, without blocking. */
  const loadEditorData = async (x: Experiment) => {
    let t: Template[] = [];
    let r: Run[] = [];
    try {
      t = await api.get<Template[]>(`/api/experiments/${encodeURIComponent(x.experiment_id)}/templates`);
    } catch {
      t = [];
    }
    try {
      r = await api.get<Run[]>(`/api/experiments/${encodeURIComponent(x.experiment_id)}/runs`);
    } catch {
      r = [];
    }
    // guarantee a Default template exists, with explicit RF values (migrate old sparse ones)
    const def = t.find((tpl) => tpl.name === 'Default');
    let sparse = false;
    if (def) {
      let p: Record<string, unknown> = {};
      try {
        p = JSON.parse(def.config_json) ?? {};
      } catch {
        sparse = true;
      }
      if (!sparse && (p.frequencyMHz === undefined || p.bandwidthMHz === undefined || p.txGainDb === undefined || p.rxGainDb === undefined)) {
        sparse = true;
      }
    }
    if (!def || sparse) {
      try {
        if (def) await api.delete(`/api/experiments/${encodeURIComponent(x.experiment_id)}/templates/${def.id}`);
        await api.post(`/api/experiments/${encodeURIComponent(x.experiment_id)}/templates`, { name: 'Default', config: DEFAULT_TEMPLATE_CONFIG });
        t = await api.get<Template[]>(`/api/experiments/${encodeURIComponent(x.experiment_id)}/templates`);
      } catch {
        /* keep whatever we had */
      }
    }
    setTemplates(t);
    setRuns(r);
    setEditorLoading(false);
  };

  /** Open the editor instantly; load data + phone state in the background. */
  const openEditor = (x: Experiment) => {
    setSelected(x);
    setEdit({ purpose: x.purpose || '', flow: x.flow || '', notes: x.notes || '' });
    setRuns([]);
    setTemplates([]);
    setEditorLoading(true);
    detectPhone(); // background, updates the badge
    loadEditorData(x);
  };

  /** Automated run: create condition + run + prepare gNB + start, with a full-screen loader. */
  const runExperiment = async (x: Experiment) => {
    const steps = ['Creating condition', 'Creating run', 'Preparing gNB config', 'Starting experiment'];
    setOverlay({ title: `Running ${x.experiment_id}`, steps, active: 0 });
    detectPhone(); // background badge update — never blocks the UI
    try {
      // use the startup template's config as the condition
      const tpls = await api.get<Template[]>(`/api/experiments/${encodeURIComponent(x.experiment_id)}/templates`).catch(() => [] as Template[]);
      const startup = tpls.find((t) => configsEqual(x.initial_oai_config, t.config_json)) ?? tpls.find((t) => t.name === 'Default') ?? tpls[0];
      const cfg: Record<string, unknown> = startup
        ? (() => {
            try {
              return JSON.parse(startup.config_json) ?? {};
            } catch {
              return {};
            }
          })()
        : { ...DEFAULT_TEMPLATE_CONFIG };

      setOverlay((o) => (o ? { ...o, active: 1 } : o));
      const conditionId = `${x.experiment_id}_auto_${Date.now()}`;
      await api.post('/api/conditions', conditionFromTemplate(conditionId, x, cfg));

      setOverlay((o) => (o ? { ...o, active: 2 } : o));
      const runId = `R_${Date.now()}`;
      await api.post<Run>('/api/runs', {
        run_id: runId,
        experiment_id: x.experiment_id,
        condition_id: conditionId,
        device_id: PHONE_SERIAL,
        start_delay_s: 30,
      });

      setOverlay((o) => (o ? { ...o, active: 3 } : o));
      const prep = await api.post<{ verify?: { ok?: boolean; problems?: string[] } }>(`/api/runs/${encodeURIComponent(runId)}/prepare`, { requested_config: cfg });
      if (prep.verify && prep.verify.ok === false) {
        const problems = (prep.verify.problems ?? []).join('; ') || 'gNB not ready';
        throw new Error(`prepare failed: ${problems}`);
      }

      setOverlay((o) => (o ? { ...o, active: 4 } : o));
      await api.post(`/api/experiments/${encodeURIComponent(x.experiment_id)}/start`, { serial: PHONE_SERIAL, run_id: runId });

      toast('ok', `run ${runId} started`);
      reload();
      nav('/dashboard');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
      reload();
    } finally {
      setOverlay(null);
    }
  };

  const openResult = (x: Experiment) => {
    detectPhone(); // background
    nav(`/timeline/${encodeURIComponent(x.experiment_id)}`);
  };

  const push = async (x: Experiment) => {
    const steps = ['Pushing task to phone'];
    setOverlay({ title: `Pushing ${x.experiment_id}`, steps, active: 0 });
    detectPhone(); // background badge update
    try {
      await api.post(`/api/experiments/${encodeURIComponent(x.experiment_id)}/push`, { serial: PHONE_SERIAL });
      toast('ok', 'Task pushed to phone');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setOverlay(null);
    }
  };

  const stopExperiment = async (x: Experiment) => {
    setOverlay({ title: `Stopping ${x.experiment_id}`, steps: ['Stopping experiment'], active: 0 });
    try {
      await api.post(`/api/experiments/${encodeURIComponent(x.experiment_id)}/stop`);
      toast('ok', 'Experiment stopped');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setOverlay(null);
    }
  };

  const doExport = async (x: Experiment) => {
    detectPhone(); // background
    setBusy('export');
    try {
      await downloadFile(`/api/experiments/${encodeURIComponent(x.experiment_id)}/export`, `${x.experiment_id}.zip`);
      toast('ok', `download started for ${x.experiment_id}`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    detectPhone(); // background
    setDeleting(true);
    try {
      await api.delete(`/api/experiments/${encodeURIComponent(deleteTarget.experiment_id)}`);
      toast('ok', `experiment ${deleteTarget.experiment_id} deleted`);
      if (selected?.experiment_id === deleteTarget.experiment_id) setSelected(null);
      setDeleteTarget(null);
      reload();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setDeleting(false);
    }
  };

  const saveTask = async () => {
    if (!selected) return;
    detectPhone(); // background
    try {
      await api.put(`/api/experiments/${encodeURIComponent(selected.experiment_id)}`, edit);
      toast('ok', 'Task saved');
      reload();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  const setTplField = (key: string, value: string) => {
    setTplModal((m) => (m ? { ...m, config: { ...m.config, [key]: value } } : m));
  };

  const saveTemplate = async () => {
    if (!selected || !tplModal) return;
    if (!tplModal.name.trim()) {
      toast('err', 'template name is required');
      return;
    }
    const invalid = validateTemplateConfig(tplModal.config);
    if (invalid) {
      toast('err', invalid);
      return;
    }
    try {
      if (tplEditing != null) {
        await api.delete(`/api/experiments/${encodeURIComponent(selected.experiment_id)}/templates/${tplEditing}`);
      }
      await api.post(`/api/experiments/${encodeURIComponent(selected.experiment_id)}/templates`, {
        name: tplModal.name.trim(),
        config: buildTemplateConfig(tplModal.config),
      });
      setTemplates(await api.get<Template[]>(`/api/experiments/${encodeURIComponent(selected.experiment_id)}/templates`));
      setTplModal(null);
      setTplEditing(null);
      toast('ok', 'Template saved');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  const deleteTemplate = async (t: Template) => {
    if (!selected) return;
    if (t.name === 'Default') {
      toast('err', 'the Default template cannot be deleted');
      return;
    }
    try {
      await api.delete(`/api/experiments/${encodeURIComponent(selected.experiment_id)}/templates/${t.id}`);
      setTemplates(templates.filter((x) => x.id !== t.id));
      toast('ok', 'Template deleted');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  /** Which template is currently wired as the experiment's startup config. */
  const isStartup = (t: Template) => configsEqual(selected?.initial_oai_config, t.config_json);

  const setStartupTemplate = async (t: Template) => {
    if (!selected) return;
    let cfg: unknown;
    try {
      cfg = JSON.parse(t.config_json);
    } catch {
      toast('err', 'template config is not valid JSON');
      return;
    }
    try {
      const updated = await api.put<Experiment>(`/api/experiments/${encodeURIComponent(selected.experiment_id)}`, { initial_oai_config: cfg });
      setSelected(updated);
      toast('ok', `startup template → ${t.name}`);
      reload();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  const deleteRun = async (runId: string) => {
    try {
      await api.delete(`/api/runs/${encodeURIComponent(runId)}`);
      setRuns(runs.filter((r) => r.run_id !== runId));
      toast('ok', `run ${runId} deleted`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  const phoneTone = phoneState === 'CONNECTED' ? 'good' : phoneState === 'ATTACHED' ? 'accent' : 'muted';

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Experiments</div>
          <div className="subtitle">create tasks, push them to the phone and run the downlink loop</div>
        </div>
        <div className="row gap-sm">
          <button className="btn" onClick={detectPhone}>Check Phone</button>
          <button className="btn primary" onClick={() => setShowCreate(true)}>+ New Experiment</button>
        </div>
      </div>

      <Card title="Phone channel">
        <div className="row gap-sm" style={{ fontSize: 12 }}>
          <Badge tone={phoneTone}>PHONE: {phoneState}</Badge>
          <span style={{ color: 'var(--muted)' }}>adb serial</span>
          <StaticValue title="Fixed by the rig — read only">{PHONE_SERIAL}</StaticValue>
          <span style={{ color: 'var(--faint)', fontSize: 11.5 }}>
            every operation re-checks this status
          </span>
        </div>
      </Card>

      <Card
        title="Experiments"
        sub={`${data?.length ?? 0} total`}
        right={<button className="btn" onClick={reload}>Refresh</button>}
      >
        {loading && !data ? (
          <Spinner />
        ) : error ? (
          <ErrorBox error={error} />
        ) : data && data.length === 0 ? (
          <EmptyState>No experiments yet — create your first one.</EmptyState>
        ) : (
          <div className="grid cols-2">
            {data?.map((x) => (
              <div key={x.experiment_id} className="card" style={{ boxShadow: 'var(--shadow-sm)' }}>
                <div className="card-header">
                  <div>
                    <h2 className="mono" style={{ fontSize: 14 }}>{x.experiment_id}</h2>
                    <div className="card-sub">{fmtIso(x.created_utc)}</div>
                  </div>
                  <Badge tone={x.environment === 'AC' || x.environment === 'RC' ? 'accent' : 'muted'}>{x.environment ?? '—'}</Badge>
                </div>
                <div className="kv" style={{ fontSize: 13 }}>
                  <dt>purpose</dt>
                  <dd>{x.purpose || '—'}</dd>
                  <dt>flow</dt>
                  <dd>{x.flow || '—'}</dd>
                  <dt>operator</dt>
                  <dd>{x.operator_name || '—'}</dd>
                </div>
                <div className="row gap-sm" style={{ marginTop: 14, flexWrap: 'wrap' }}>
                  <button className="btn sm" disabled={busy !== null} onClick={() => openEditor(x)}>Edit</button>
                  <button className="btn sm" disabled={busy !== null} onClick={() => push(x)}>Push</button>
                  <button className="btn sm primary" disabled={busy !== null} onClick={() => runExperiment(x)}>Run</button>
                  <button className="btn sm" disabled={busy !== null} onClick={() => openResult(x)}>Result</button>
                  <button className="btn sm" disabled={busy !== null} onClick={() => doExport(x)}>Export</button>
                  <button className="btn sm danger" disabled={busy !== null} onClick={() => setDeleteTarget(x)}>Delete</button>
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Create experiment (floating window) */}
      {showCreate && (
        <Modal
          title="New Experiment"
          sub="register an experiment task in the platform"
          onClose={() => setShowCreate(false)}
          footer={
            <>
              <button className="btn" onClick={() => setShowCreate(false)}>Cancel</button>
              <button className="btn primary" form="create-exp-form" type="submit" disabled={creating || !form.experiment_id.trim()}>
                {creating ? 'Creating…' : 'Create Experiment'}
              </button>
            </>
          }
        >
          <form id="create-exp-form" className="stack" onSubmit={submit}>
            <div className="grid cols-2">
              <Field label="Experiment ID (required)">
                <input
                  className="mono"
                  value={form.experiment_id}
                  placeholder="e.g. AC_2026_08_15_01"
                  autoFocus
                  onChange={(e) => setForm({ ...form, experiment_id: e.target.value })}
                />
              </Field>
              <Field label="Environment">
                <select value={form.environment} onChange={(e) => setForm({ ...form, environment: e.target.value })}>
                  <option value="AC">AC</option>
                  <option value="RC">RC</option>
                </select>
              </Field>
              <Field label="Operator">
                <input value={form.operator_name} onChange={(e) => setForm({ ...form, operator_name: e.target.value })} />
              </Field>
              <Field label="Notes">
                <input value={form.notes} onChange={(e) => setForm({ ...form, notes: e.target.value })} />
              </Field>
            </div>
            <Field label="Purpose">
              <textarea rows={2} value={form.purpose} onChange={(e) => setForm({ ...form, purpose: e.target.value })} />
            </Field>
            <Field label="Flow">
              <textarea rows={2} value={form.flow} onChange={(e) => setForm({ ...form, flow: e.target.value })} />
            </Field>
          </form>
        </Modal>
      )}

      {/* Task editor (floating window) */}
      {selected && (
        <Modal
          title={`Edit · ${selected.experiment_id}`}
          sub={`${selected.environment ?? '—'} · operator ${selected.operator_name || '—'}`}
          onClose={() => setSelected(null)}
          size="lg"
        >
          <div className="stack">
            <div className="grid cols-2">
              <Field label="Purpose">
                <textarea rows={3} value={edit.purpose} onChange={(e) => setEdit({ ...edit, purpose: e.target.value })} />
              </Field>
              <Field label="Flow">
                <textarea rows={3} value={edit.flow} onChange={(e) => setEdit({ ...edit, flow: e.target.value })} />
              </Field>
              <Field label="Notes">
                <input value={edit.notes} onChange={(e) => setEdit({ ...edit, notes: e.target.value })} />
              </Field>
            </div>
            <div className="row gap-sm">
              <button className="btn primary" onClick={saveTask}>Save Task</button>
              <button className="btn danger" onClick={() => stopExperiment(selected)}>Stop Experiment</button>
            </div>

            <Card
              title="Templates"
              sub="click a template to use it at startup"
              right={
                <button
                  className="btn sm"
                  onClick={() => {
                    const base = templates.find(isStartup) ?? templates.find((t) => t.name === 'Default') ?? templates[0];
                    setTplModal({
                      name: '',
                      config: base ? configJsonToForm(base.config_json) : configJsonToForm(JSON.stringify(DEFAULT_TEMPLATE_CONFIG)),
                    });
                  }}
                >
                  + Add Template
                </button>
              }
            >
              {editorLoading ? (
                <Spinner label="loading templates & runs…" />
              ) : templates.length === 0 ? (
                <EmptyState>No templates.</EmptyState>
              ) : (
                <div className="stack" style={{ gap: 10 }}>
                  {templates.map((t) => {
                    const props = configSummary(t.config_json);
                    const startup = isStartup(t);
                    return (
                      <div
                        key={t.id}
                        className="card"
                        style={{
                          padding: 12,
                          cursor: 'pointer',
                          border: startup ? '1px solid var(--accent)' : undefined,
                          background: startup ? 'var(--accent-soft)' : undefined,
                        }}
                        title="Click to use this template at startup"
                        onClick={() => setStartupTemplate(t)}
                      >
                        <div className="row between">
                          <div className="row gap-sm">
                            <b style={{ fontSize: 13 }}>{t.name}</b>
                            {t.name === 'Default' && <Badge tone="muted">Default</Badge>}
                            {startup && <Badge tone="accent">startup</Badge>}
                          </div>
                          <div className="row gap-sm" onClick={(e) => e.stopPropagation()}>
                            <button
                              className="btn sm"
                              onClick={() => {
                                setTplModal({ name: t.name, config: configJsonToForm(t.config_json) });
                                setTplEditing(t.id);
                              }}
                            >
                              Edit
                            </button>
                            <button className="btn sm danger" onClick={() => deleteTemplate(t)}>Delete</button>
                          </div>
                        </div>
                        {props.length === 0 ? (
                          <div style={{ fontSize: 12, color: 'var(--faint)', marginTop: 8 }}>no config set</div>
                        ) : (
                          <div className="row gap-sm" style={{ marginTop: 8, fontSize: 11.5 }}>
                            {props.map((p) => (
                              <span key={p.key} className="badge muted">{p.label} = {p.value}</span>
                            ))}
                          </div>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </Card>

            <Card title="Results" sub={`${runs.length} run(s)`}>
              {editorLoading ? (
                <Spinner label="loading runs…" />
              ) : runs.length === 0 ? (
                <EmptyState>No results yet.</EmptyState>
              ) : (
                <div className="grid cols-3">
                  {runs.map((r) => (
                    <div key={r.run_id} className="card" style={{ padding: 12 }}>
                      <div className="row between">
                        <span className="mono" style={{ fontSize: 12, wordBreak: 'break-all' }}>{r.run_id}</span>
                        <button className="btn sm danger" onClick={() => deleteRun(r.run_id)}>Delete</button>
                      </div>
                      <div className="row gap-sm" style={{ marginTop: 8 }}>
                        <Badge tone={r.state === 'COMPLETE' ? 'good' : r.state === 'FAILED' ? 'bad' : r.state === 'WARNING' ? 'warn' : 'muted'}>
                          {r.state}
                        </Badge>
                        {r.quality_status && (
                          <Badge tone={r.quality_status === 'PASS' ? 'good' : r.quality_status === 'FAILED' ? 'bad' : 'warn'}>
                            {r.quality_status}
                          </Badge>
                        )}
                      </div>
                      <button className="btn sm" style={{ marginTop: 8 }} onClick={() => nav(`/timeline/${encodeURIComponent(r.experiment_id)}`)}>
                        View detail →
                      </button>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </Modal>
      )}

      {/* Delete confirm (floating window) */}
      {deleteTarget && (
        <Modal
          title="Delete Experiment"
          sub={deleteTarget.experiment_id}
          onClose={() => setDeleteTarget(null)}
          footer={
            <>
              <button className="btn" onClick={() => setDeleteTarget(null)}>Cancel</button>
              <button className="btn danger" disabled={deleting} onClick={confirmDelete}>
                {deleting ? 'Deleting…' : 'Delete'}
              </button>
            </>
          }
        >
          <div className="notice-box">
            This permanently removes the experiment, its conditions, runs, templates and derived files. This cannot be undone.
          </div>
        </Modal>
      )}

      {/* Template editor (floating window, structured OAI fields) */}
      {tplModal &&
        (() => {
          const c = tplModal.config;
          const puschManual = c.puschTargetMode === 'manual';
          const schedManual = c.schedulerMode === 'manual';
          return (
            <Modal
              title={tplEditing != null ? 'Edit Template' : 'Add Template'}
              sub={selected?.experiment_id}
              onClose={() => {
                setTplModal(null);
                setTplEditing(null);
              }}
              size="lg"
              footer={<button className="btn primary" onClick={saveTemplate}>Save Template</button>}
            >
              <div className="stack">
                <Field label="Template name">
                  <input value={tplModal.name} placeholder="e.g. my_template" onChange={(e) => setTplModal({ ...tplModal, name: e.target.value })} />
                </Field>

                <h3 style={{ margin: 0 }}>RF / gNB</h3>
                <div className="grid cols-3">
                  <Field label="Frequency (MHz)">
                    <input className="mono" type="number" value={c.frequencyMHz} onChange={(e) => setTplField('frequencyMHz', e.target.value)} />
                  </Field>
                  <Field label="Bandwidth (MHz)">
                    <input className="mono" type="number" value={c.bandwidthMHz} onChange={(e) => setTplField('bandwidthMHz', e.target.value)} />
                  </Field>
                  <Field label="TX gain (dB)">
                    <input className="mono" type="number" value={c.txGainDb} onChange={(e) => setTplField('txGainDb', e.target.value)} />
                  </Field>
                  <Field label="RX gain (dB)">
                    <input className="mono" type="number" value={c.rxGainDb} onChange={(e) => setTplField('rxGainDb', e.target.value)} />
                  </Field>
                </div>

                <h3 style={{ margin: 0 }}>PUSCH target</h3>
                <div className="grid cols-3">
                  <Field label="PUSCH target mode">
                    <select
                      value={c.puschTargetMode}
                      onChange={(e) => {
                        const v = e.target.value;
                        setTplModal((m) => {
                          if (!m) return m;
                          const config: Record<string, string> = { ...m.config, puschTargetMode: v };
                          if (v === 'manual' && !config.puschTargetSnrX10) config.puschTargetSnrX10 = '89';
                          return { ...m, config };
                        });
                      }}
                    >
                      <option value="auto">auto</option>
                      <option value="manual">manual</option>
                    </select>
                  </Field>
                  {puschManual && (
                    <Field label="PUSCH target SNR (×10)">
                      <input className="mono" type="number" value={c.puschTargetSnrX10} onChange={(e) => setTplField('puschTargetSnrX10', e.target.value)} />
                    </Field>
                  )}
                </div>

                <h3 style={{ margin: 0 }}>UL scheduler</h3>
                <div className="grid cols-3">
                  <Field label="Scheduler mode">
                    <select
                      value={c.schedulerMode}
                      onChange={(e) => {
                        const v = e.target.value;
                        setTplModal((m) => {
                          if (!m) return m;
                          const config: Record<string, string> = { ...m.config, schedulerMode: v };
                          if (v === 'manual') {
                            if (!config.qm) config.qm = '2';
                            if (!config.mcs) config.mcs = '0';
                            if (!config.nPrb) config.nPrb = '273';
                          }
                          return { ...m, config };
                        });
                      }}
                    >
                      <option value="auto">auto</option>
                      <option value="manual">manual</option>
                    </select>
                  </Field>
                  {schedManual && (
                    <>
                      <Field label="Qm (modulation)">
                        <select
                          value={c.qm}
                          onChange={(e) => {
                            const v = e.target.value;
                            setTplModal((m) => {
                              if (!m) return m;
                              const config: Record<string, string> = { ...m.config, qm: v };
                              const r = QM_MCS_RANGE[v];
                              const cur = Number(config.mcs);
                              if (r && (config.mcs === '' || !Number.isFinite(cur) || cur < r.min || cur > r.max)) {
                                config.mcs = String(r.min);
                              }
                              return { ...m, config };
                            });
                          }}
                        >
                          {QM_OPTIONS.map((o) => (
                            <option key={o.value} value={o.value}>{o.label}</option>
                          ))}
                        </select>
                      </Field>
                      <Field label="MCS" hint={mcsRangeHint(c.qm)}>
                        <input className="mono" type="number" value={c.mcs} placeholder={mcsRangeHint(c.qm)} onChange={(e) => setTplField('mcs', e.target.value)} />
                      </Field>
                      <Field label="N_PRB">
                        <input className="mono" type="number" value={c.nPrb} onChange={(e) => setTplField('nPrb', e.target.value)} />
                      </Field>
                    </>
                  )}
                </div>
              </div>
            </Modal>
          );
        })()}

      {overlay && <FullScreenLoader title={overlay.title} steps={overlay.steps} active={overlay.active} />}
    </div>
  );
}
