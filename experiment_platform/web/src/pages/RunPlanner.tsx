import { useMemo, useState } from 'react';
import { api } from '../api';
import type { Condition, ConfigComparisonRow, Experiment, PrepareConfigResult, PrepareResult, Run, Templates } from '../types';
import { Badge, Card, Field, toast } from '../components/ui';
import { useLoad } from '../components/DataView';

const NUMERIC_FIELDS = new Set([
  'orientation_deg',
  'incident_power_density_wm2',
  'target_rsrp_dbm',
  'frequency_mhz',
  'bandwidth_mhz',
  'tx_gain_db',
  'rx_gain_db',
  'pusch_target_snr_db',
  'pusch_target_snr_x10',
  'mcs',
  'qm',
  'n_prb',
]);

const EMPTY_COND: Record<string, string> = {
  environment: 'AC',
  orientation_deg: '',
  incident_power_density_wm2: '',
  stirrer_mode: '',
  stirrer_state: '',
  target_rsrp_dbm: '',
  traffic_condition: '',
  frequency_mhz: '3349.92',
  bandwidth_mhz: '100',
  tx_gain_db: '60',
  rx_gain_db: '40',
  pusch_target_mode: 'manual',
  pusch_target_snr_db: '',
  pusch_target_snr_x10: '89',
  scheduler_mode: 'auto',
  mcs: '',
  qm: '',
  n_prb: '',
  chamber_metadata: '',
};

/** OAI config keys the backend `apply_condition` understands (backend/oai_client.py). */
function requestedConfigFromForm(f: Record<string, string>): Record<string, unknown> {
  const cfg: Record<string, unknown> = {};
  const putNum = (key: string, src: string | undefined) => {
    if (src !== undefined && src !== '') {
      const n = Number(src);
      cfg[key] = Number.isFinite(n) ? n : src;
    }
  };
  putNum('frequencyMHz', f.frequency_mhz);
  putNum('bandwidthMHz', f.bandwidth_mhz);
  putNum('txGainDb', f.tx_gain_db);
  putNum('rxGainDb', f.rx_gain_db);
  if (f.pusch_target_mode) cfg['puschTargetMode'] = f.pusch_target_mode;
  putNum('puschTargetSnrX10', f.pusch_target_snr_x10);
  if (f.scheduler_mode) cfg['schedulerMode'] = f.scheduler_mode;
  putNum('mcs', f.mcs);
  putNum('qm', f.qm);
  putNum('nPrb', f.n_prb);
  return cfg;
}

function toConditionPayload(conditionId: string, experimentId: string, f: Record<string, string>): Record<string, unknown> {
  const payload: Record<string, unknown> = { condition_id: conditionId, experiment_id: experimentId };
  for (const [k, v] of Object.entries(f)) {
    if (k === 'chamber_metadata') {
      if (v.trim()) {
        try {
          payload['chamber_metadata'] = JSON.parse(v);
        } catch {
          payload['chamber_metadata'] = { raw: v };
        }
      }
      continue;
    }
    if (v === '') continue;
    if (NUMERIC_FIELDS.has(k)) {
      const n = Number(v);
      if (Number.isFinite(n)) payload[k] = n;
    } else {
      payload[k] = v;
    }
  }
  return payload;
}

function formFromCondition(c: Condition): Record<string, string> {
  const f: Record<string, string> = { ...EMPTY_COND };
  for (const k of Object.keys(EMPTY_COND)) {
    const v = (c as Record<string, unknown>)[k];
    if (v !== null && v !== undefined) f[k] = String(v);
  }
  if (c.chamber_metadata_json) {
    try {
      f.chamber_metadata = JSON.stringify(JSON.parse(c.chamber_metadata_json), null, 2);
    } catch {
      f.chamber_metadata = c.chamber_metadata_json;
    }
  }
  return f;
}

/** Extract a comparable "actual" view from the OAI research/config raw JSON. */
function extractActual(actual: Record<string, unknown> | null | undefined): Record<string, unknown> {
  const a: Record<string, unknown> = actual ?? {};
  const controls = (a['controls'] ?? {}) as Record<string, unknown>;
  const ulSched = (controls['ulScheduler'] ?? {}) as Record<string, unknown>;
  return {
    frequencyMHz: a['frequencyMHz'],
    bandwidthMHz: a['bandwidthMHz'],
    txGainDb: a['txGainDb'],
    rxGainDb: a['rxGainDb'],
    puschTargetMode: a['puschTargetMode'],
    puschTargetSnrX10: a['puschTargetSnrX10'],
    schedulerMode: a['ulSchedulerMode'],
    mcs: ulSched['mcs'],
    qm: ulSched['qm'],
    nPrb: ulSched['nPrb'],
  };
}

function snrDb(x10: unknown): string {
  if (x10 === null || x10 === undefined || x10 === '') return '—';
  const n = Number(x10);
  return Number.isFinite(n) ? (n / 10).toFixed(1) : String(x10);
}

function buildComparisonRows(req: Record<string, unknown>, act: Record<string, unknown>): ConfigComparisonRow[] {
  const defs: { key: string; label: string; fmt: (v: unknown) => string }[] = [
    { key: 'frequencyMHz', label: 'frequency (MHz)', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'bandwidthMHz', label: 'bandwidth (MHz)', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'txGainDb', label: 'TX gain (dB)', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'rxGainDb', label: 'RX gain (dB)', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'puschTargetMode', label: 'PUSCH target mode', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'puschTargetSnrX10', label: 'PUSCH target SNR (dB)', fmt: snrDb },
    { key: 'schedulerMode', label: 'scheduler mode', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'mcs', label: 'MCS', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'qm', label: 'Qm', fmt: (v) => (v == null ? '—' : String(v)) },
    { key: 'nPrb', label: 'N_PRB', fmt: (v) => (v == null ? '—' : String(v)) },
  ];
  return defs.map((d) => {
    const rv = d.fmt(req[d.key]);
    const av = d.fmt(act[d.key]);
    const rRaw = req[d.key];
    const aRaw = act[d.key];
    const mismatch = rRaw != null && aRaw != null && String(rRaw) !== String(aRaw);
    return { key: d.key, label: d.label, requested: rv, actual: av, mismatch };
  });
}

export default function RunPlanner({ nav, initialExperimentId }: { nav: (p: string) => void; initialExperimentId?: string }) {
  const expLoad = useLoad<Experiment[]>(() => api.get('/api/experiments'), []);
  const tplLoad = useLoad<Templates>(() => api.get('/api/templates'), []);

  const [experimentId, setExperimentId] = useState(initialExperimentId ?? '');
  const [conditionId, setConditionId] = useState('');
  const [condForm, setCondForm] = useState<Record<string, string>>({ ...EMPTY_COND });
  const [template, setTemplate] = useState('');
  const [sweepList, setSweepList] = useState('50, 89, 120, 200');

  const condLoad = useLoad<Condition[]>(
    () => (experimentId ? api.get(`/api/conditions?experiment_id=${encodeURIComponent(experimentId)}`) : Promise.resolve([])),
    [experimentId],
  );

  const [runForm, setRunForm] = useState({ run_id: '', device_id: '', session_id: '', planned_order: '', random_seed: '', start_delay_s: '30' });
  const [run, setRun] = useState<Run | null>(null);

  const [busy, setBusy] = useState<string | null>(null);

  const [preview, setPreview] = useState<PrepareConfigResult | null>(null);
  const [prepare, setPrepare] = useState<PrepareResult | null>(null);

  const requested = useMemo(() => requestedConfigFromForm(condForm), [condForm]);
  const activeTemplate = template ? tplLoad.data?.[template] : undefined;

  const selectCondition = (id: string) => {
    setConditionId(id);
    const c = condLoad.data?.find((x) => x.condition_id === id);
    if (c) setCondForm(formFromCondition(c));
  };

  const selectExperiment = (id: string) => {
    setExperimentId(id);
    setConditionId('');
    setCondForm((f) => ({ ...f, environment: expLoad.data?.find((e) => e.experiment_id === id)?.environment ?? f.environment }));
  };

  const saveCondition = async () => {
    if (!experimentId || !conditionId.trim()) return toast('err', 'experiment and condition ID required');
    setBusy('saveCondition');
    try {
      await api.post('/api/conditions', toConditionPayload(conditionId.trim(), experimentId, condForm));
      toast('ok', `condition ${conditionId.trim()} saved`);
      condLoad.reload();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const generateSweep = async () => {
    if (!experimentId) return toast('err', 'select experiment first');
    const base = conditionId.trim() || `sweep_${experimentId}`;
    const values = sweepList.split(',').map((s) => s.trim()).filter(Boolean).map(Number).filter(Number.isFinite);
    if (values.length === 0) return toast('err', 'no sweep values');
    setBusy('sweep');
    try {
      const created: string[] = [];
      for (const v of values) {
        const f = { ...condForm, pusch_target_mode: 'manual', pusch_target_snr_x10: String(v) };
        const id = `${base}_snr${v}`;
        await api.post('/api/conditions', toConditionPayload(id, experimentId, f));
        created.push(id);
      }
      toast('ok', `created ${created.length} conditions: ${created.join(', ')}`);
      condLoad.reload();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const createRun = async () => {
    if (!experimentId || !conditionId.trim() || !runForm.run_id.trim()) return toast('err', 'experiment, condition and run_id required');
    setBusy('createRun');
    try {
      const r = await api.post<Run>('/api/runs', {
        run_id: runForm.run_id.trim(),
        experiment_id: experimentId,
        condition_id: conditionId.trim(),
        device_id: runForm.device_id || null,
        session_id: runForm.session_id || null,
        planned_order: runForm.planned_order ? Number(runForm.planned_order) : null,
        random_seed: runForm.random_seed ? Number(runForm.random_seed) : null,
        start_delay_s: runForm.start_delay_s ? Number(runForm.start_delay_s) : 30,
      });
      setRun(r);
      toast('ok', `run ${r.run_id} created (state ${r.state})`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const runIdForOps = run?.run_id || runForm.run_id.trim() || 'preview';

  const doPreview = async () => {
    setBusy('preview');
    setPrepare(null);
    try {
      const r = await api.post<PrepareConfigResult>(`/api/runs/${encodeURIComponent(runIdForOps)}/prepare-config`, { requested_config: requested });
      setPreview(r);
      toast('ok', 'preview computed (dry-run, no change applied)');
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const doPrepare = async () => {
    if (!run) return toast('err', 'create the run first (Prepare applies config to a real run)');
    setBusy('prepare');
    setPreview(null);
    try {
      const r = await api.post<PrepareResult>(`/api/runs/${encodeURIComponent(run.run_id)}/prepare`, { requested_config: requested });
      setPrepare(r);
      toast('ok', r.verify?.ok ? 'prepare OK — gNB ready' : `prepare finished with problems: ${(r.verify?.problems ?? []).join('; ')}`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  const setField = (k: string, v: string) => setCondForm((f) => ({ ...f, [k]: v }));

  const compRows = preview ? buildComparisonRows(preview.requested, extractActual(preview.actual)) : buildComparisonRows(requested, {});
  const prepareActual = prepare?.actual ? extractActual(prepare.actual as Record<string, unknown>) : null;

  return (
    <div className="stack">
      <Card title="1 · Experiment, Template & Condition">
        <div className="grid cols-3">
          <Field label="Experiment">
            <select value={experimentId} onChange={(e) => selectExperiment(e.target.value)}>
              <option value="">— select —</option>
              {expLoad.data?.map((x) => (
                <option key={x.experiment_id} value={x.experiment_id}>
                  {x.experiment_id} ({x.environment})
                </option>
              ))}
            </select>
          </Field>
          <Field label="Condition (select to load / type new)">
            <input
              className="mono"
              list="cond-options"
              value={conditionId}
              placeholder="condition_id"
              onChange={(e) => setConditionId(e.target.value)}
              onBlur={() => {
                const c = condLoad.data?.find((x) => x.condition_id === conditionId);
                if (c) setCondForm(formFromCondition(c));
              }}
            />
            <datalist id="cond-options">
              {condLoad.data?.map((c) => (
                <option key={c.condition_id} value={c.condition_id} />
              ))}
            </datalist>
          </Field>
          <Field label="Template">
            <select value={template} onChange={(e) => setTemplate(e.target.value)}>
              <option value="">— none —</option>
              {Object.entries(tplLoad.data ?? {}).map(([k]) => (
                <option key={k} value={k}>
                  {k}
                </option>
              ))}
            </select>
          </Field>
        </div>
        {activeTemplate && (
          <div className="notice-box" style={{ marginTop: 12 }}>
            <b>{template}</b>: {activeTemplate.description}
            <br />
            <span className="mono">vary: {activeTemplate.vary.join(', ') || '—'}</span>
            <br />
            <span className="mono">fixed: {activeTemplate.fixed.join(', ') || '—'}</span>
          </div>
        )}
        <div className="row" style={{ marginTop: 12, alignItems: 'flex-end' }}>
          <Field label="PUSCH target sweep (X10 values, comma-separated)">
            <input className="mono" style={{ width: 260 }} value={sweepList} onChange={(e) => setSweepList(e.target.value)} />
          </Field>
          <button className="btn" disabled={busy === 'sweep' || !experimentId} onClick={generateSweep}>
            {busy === 'sweep' ? 'creating…' : 'Generate sweep conditions'}
          </button>
          <button className="btn primary" disabled={busy === 'saveCondition'} onClick={saveCondition}>
            {busy === 'saveCondition' ? 'saving…' : 'Save condition'}
          </button>
        </div>
      </Card>

      <Card title="2 · Condition fields">
        <h3>RF / gNB</h3>
        <div className="grid cols-4">
          <Field label="frequency_mhz"><input className="mono" value={condForm.frequency_mhz} onChange={(e) => setField('frequency_mhz', e.target.value)} /></Field>
          <Field label="bandwidth_mhz"><input className="mono" value={condForm.bandwidth_mhz} onChange={(e) => setField('bandwidth_mhz', e.target.value)} /></Field>
          <Field label="tx_gain_db"><input className="mono" value={condForm.tx_gain_db} onChange={(e) => setField('tx_gain_db', e.target.value)} /></Field>
          <Field label="rx_gain_db"><input className="mono" value={condForm.rx_gain_db} onChange={(e) => setField('rx_gain_db', e.target.value)} /></Field>
          <Field label="pusch_target_mode">
            <select value={condForm.pusch_target_mode} onChange={(e) => setField('pusch_target_mode', e.target.value)}>
              <option value="auto">auto</option>
              <option value="manual">manual</option>
            </select>
          </Field>
          <Field label="pusch_target_snr_x10"><input className="mono" value={condForm.pusch_target_snr_x10} onChange={(e) => setField('pusch_target_snr_x10', e.target.value)} /></Field>
          <Field label="pusch_target_snr_db"><input className="mono" value={condForm.pusch_target_snr_db} onChange={(e) => setField('pusch_target_snr_db', e.target.value)} /></Field>
          <Field label="scheduler_mode">
            <select value={condForm.scheduler_mode} onChange={(e) => setField('scheduler_mode', e.target.value)}>
              <option value="auto">auto</option>
              <option value="manual">manual</option>
            </select>
          </Field>
          <Field label="mcs"><input className="mono" value={condForm.mcs} onChange={(e) => setField('mcs', e.target.value)} /></Field>
          <Field label="qm"><input className="mono" value={condForm.qm} onChange={(e) => setField('qm', e.target.value)} /></Field>
          <Field label="n_prb"><input className="mono" value={condForm.n_prb} onChange={(e) => setField('n_prb', e.target.value)} /></Field>
        </div>

        <h3 style={{ marginTop: 16 }}>AC / RC chamber</h3>
        <div className="grid cols-4">
          <Field label="environment">
            <select value={condForm.environment} onChange={(e) => setField('environment', e.target.value)}>
              <option value="AC">AC</option>
              <option value="RC">RC</option>
            </select>
          </Field>
          <Field label="orientation_deg"><input className="mono" value={condForm.orientation_deg} onChange={(e) => setField('orientation_deg', e.target.value)} /></Field>
          <Field label="incident_power_density_wm2"><input className="mono" value={condForm.incident_power_density_wm2} onChange={(e) => setField('incident_power_density_wm2', e.target.value)} /></Field>
          <Field label="target_rsrp_dbm"><input className="mono" value={condForm.target_rsrp_dbm} onChange={(e) => setField('target_rsrp_dbm', e.target.value)} /></Field>
          <Field label="stirrer_mode">
            <select value={condForm.stirrer_mode} onChange={(e) => setField('stirrer_mode', e.target.value)}>
              <option value="">—</option>
              <option value="step">step</option>
              <option value="continuous">continuous</option>
            </select>
          </Field>
          <Field label="stirrer_state"><input className="mono" value={condForm.stirrer_state} onChange={(e) => setField('stirrer_state', e.target.value)} /></Field>
          <Field label="traffic_condition"><input className="mono" value={condForm.traffic_condition} onChange={(e) => setField('traffic_condition', e.target.value)} /></Field>
        </div>
        <div style={{ marginTop: 12 }}>
          <Field label="chamber_metadata (JSON)">
            <textarea className="mono" rows={5} value={condForm.chamber_metadata} onChange={(e) => setField('chamber_metadata', e.target.value)} />
          </Field>
        </div>
      </Card>

      <Card title="3 · Run">
        <div className="grid cols-4">
          <Field label="run_id (required)"><input className="mono" value={runForm.run_id} onChange={(e) => setRunForm({ ...runForm, run_id: e.target.value })} placeholder="e.g. R001" /></Field>
          <Field label="device_id"><input className="mono" value={runForm.device_id} onChange={(e) => setRunForm({ ...runForm, device_id: e.target.value })} /></Field>
          <Field label="session_id"><input className="mono" value={runForm.session_id} onChange={(e) => setRunForm({ ...runForm, session_id: e.target.value })} /></Field>
          <Field label="planned_order"><input className="mono" value={runForm.planned_order} onChange={(e) => setRunForm({ ...runForm, planned_order: e.target.value })} /></Field>
          <Field label="random_seed"><input className="mono" value={runForm.random_seed} onChange={(e) => setRunForm({ ...runForm, random_seed: e.target.value })} /></Field>
          <Field label="start_delay_s"><input className="mono" value={runForm.start_delay_s} onChange={(e) => setRunForm({ ...runForm, start_delay_s: e.target.value })} /></Field>
          <div style={{ alignSelf: 'end' }}>
            <button className="btn primary" disabled={busy === 'createRun'} onClick={createRun}>
              {busy === 'createRun' ? 'creating…' : 'Create run'}
            </button>
          </div>
        </div>
        {run && (
          <div className="row gap-sm" style={{ marginTop: 12 }}>
            <Badge tone="accent">run {run.run_id}</Badge>
            <Badge tone="muted">state {run.state}</Badge>
            <button className="btn" onClick={() => nav(`/run/${encodeURIComponent(run.run_id)}`)}>open Run Detail →</button>
          </div>
        )}
      </Card>

      <Card
        title="4 · Requested vs Actual config"
        right={
          <div className="row gap-sm">
            <button className="btn" disabled={busy === 'preview'} onClick={doPreview}>
              {busy === 'preview' ? 'previewing…' : 'Preview config'}
            </button>
            <button className="btn primary" disabled={busy === 'prepare' || !run} onClick={doPrepare} title={run ? '' : 'create the run first'}>
              {busy === 'prepare' ? 'preparing…' : 'Prepare Run'}
            </button>
          </div>
        }
      >
        <div className="two-col">
          <div>
            <div className="row gap-sm" style={{ marginBottom: 8 }}>
              <Badge>requested</Badge>
              {preview && <Badge tone="accent">from prepare-config (dry-run)</Badge>}
            </div>
            <pre className="raw">{JSON.stringify(requested, null, 2)}</pre>
          </div>
          <div>
            <div className="row gap-sm" style={{ marginBottom: 8 }}>
              <Badge>actual</Badge>
              {preview ? (
                <Badge tone="accent">from /api/oai/research/config</Badge>
              ) : (
                <Badge tone="muted">run preview to fetch live config</Badge>
              )}
            </div>
            <pre className="raw">
              {preview ? JSON.stringify(extractActual(preview.actual), null, 2) : '—'}
            </pre>
          </div>
        </div>

        <h3 style={{ margin: '16px 0 8px' }}>Comparison</h3>
        <div className="table-wrap">
          <table className="data">
            <thead>
              <tr>
                <th>Field</th>
                <th>Requested</th>
                <th>Actual</th>
                <th></th>
              </tr>
            </thead>
            <tbody>
              {compRows.map((r) => (
                <tr key={r.key} className={r.mismatch ? 'warning' : undefined}>
                  <td className="mono">{r.label}</td>
                  <td className="mono">{r.requested}</td>
                  <td className="mono">{r.actual}</td>
                  <td>{r.mismatch ? <Badge tone="warn">diff</Badge> : <Badge tone="good">ok</Badge>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {prepareActual && (
          <div className="notice-box" style={{ marginTop: 12 }}>
            <b>Prepare result</b> — verify {prepare?.verify?.ok ? 'OK' : 'FAILED'}
            {prepare?.verify?.problems?.length ? `: ${prepare.verify.problems.join('; ')}` : ''}
          </div>
        )}
      </Card>
    </div>
  );
}
