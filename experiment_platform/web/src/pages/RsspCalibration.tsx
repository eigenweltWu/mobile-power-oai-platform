import { useCallback, useEffect, useRef, useState } from 'react';
import { Legend, Line, LineChart, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { api } from '../api';
import { Badge, Card, ErrorBox, Field, StatCard, toast } from '../components/ui';

type CalibrationPoint = {
  ms: number; iter: number; measurement: string; metric_label: string; unit: string;
  observed_db: number; target_db: number; err_db: number;
  tx_gain_db?: number; rx_gain_db?: number; target_snr_db?: number;
  applied_actuator?: string; applied_value?: number; applied_unit?: string;
  settle_sample_count?: number;
};
type CalibrationStatus = {
  running: boolean; state: string; measurement?: string; metric_label?: string;
  actuator?: string; unit?: string; target_db?: number; tolerance_db?: number;
  pusch_x10?: number; tx_gain_db?: number; rx_gain_db?: number; last_value_db?: number;
  initial_pusch_mode?: string; initial_pusch_x10?: number;
  records: CalibrationPoint[]; log: { ms: number; stage: string; msg: string }[];
  error?: string; restore_error?: string;
};
type PlatformHealth = { oai?: { ok?: boolean; error?: string } };
type GnbConfiguration = {
  frequencyMHz: number; bandwidthMHz: number; txGainDb: number; rxGainDb: number;
  puschTargetMode: 'manual'; puschTargetSnrDb: number;
  schedulerMode: 'auto' | 'manual'; qm: number; mcs: number; nPrb: number;
};
type GnbStatus = {
  gnb: { running?: boolean; status?: string; startedAt?: string; exitCode?: number };
  radio: Record<string, unknown>;
  configuration: Record<string, unknown>;
  supportedBandwidthMHz?: number[];
  telemetry: { available?: boolean; freshUeCount?: number; ueRsrpDbm?: number; puschRssiDbfs?: number };
};

const DEFAULT_GNB: GnbConfiguration = {
  frequencyMHz: 3349.92, bandwidthMHz: 100, txGainDb: 60, rxGainDb: 40,
  puschTargetMode: 'manual', puschTargetSnrDb: 8.9,
  schedulerMode: 'auto', qm: 8, mcs: 27, nPrb: 50,
};
const TABLE2_MCS_RANGE: Record<number, [number, number]> = {
  2: [0, 4], 4: [5, 10], 6: [11, 19], 8: [20, 27],
};
const terminalGood = new Set(['converged']);
const terminalWarn = new Set(['exhausted', 'stopped']);

export default function RsspCalibration({ onBack }: { onBack: () => void }) {
  const [status, setStatus] = useState<CalibrationStatus>({ running: false, state: 'idle', records: [], log: [] });
  const [health, setHealth] = useState<PlatformHealth>();
  const [gnb, setGnb] = useState<GnbStatus>();
  const [error, setError] = useState<unknown>();
  const [busy, setBusy] = useState('');
  const [gnbForm, setGnbForm] = useState<GnbConfiguration>(DEFAULT_GNB);
  const hydrated = useRef(false);
  const calibrationWasRunning = useRef(false);
  const [form, setForm] = useState({
    measurement: 'pusch_rssi' as 'ue_rsrp' | 'pusch_rssi',
    actuator: 'target_snr' as 'tx_gain' | 'rx_gain' | 'target_snr',
    target_db: '-60', tolerance_db: 1.5, gain_alpha: 0.5,
    max_servo_iters: 6, listening_period_s: 90, settle_time_s: 5,
  });

  const load = useCallback(async () => {
    const [statusResult, healthResult, gnbResult] = await Promise.allSettled([
      api.get<CalibrationStatus>('/api/diagnostics/rssp-calibration/status'),
      api.get<PlatformHealth>('/api/platform/health'),
      api.get<GnbStatus>('/api/diagnostics/rssp-calibration/gnb'),
    ]);
    if (statusResult.status === 'fulfilled') {
      const next = statusResult.value;
      setStatus(next);
      if (next.running || calibrationWasRunning.current) setGnbForm((current) => ({
        ...current,
        txGainDb: next.tx_gain_db ?? current.txGainDb,
        rxGainDb: next.rx_gain_db ?? current.rxGainDb,
        puschTargetSnrDb: next.pusch_x10 == null ? current.puschTargetSnrDb : next.pusch_x10 / 10,
      }));
      calibrationWasRunning.current = next.running;
    }
    if (healthResult.status === 'fulfilled') setHealth(healthResult.value);
    else setHealth({ oai: { ok: false, error: healthResult.reason instanceof Error ? healthResult.reason.message : String(healthResult.reason) } });
    if (gnbResult.status === 'fulfilled') {
      setGnb(gnbResult.value);
      if (!hydrated.current) {
        const c = gnbResult.value.configuration;
        const qm = TABLE2_MCS_RANGE[Number(c.qm)] ? Number(c.qm) : DEFAULT_GNB.qm;
        const [mcsLow, mcsHigh] = TABLE2_MCS_RANGE[qm];
        setGnbForm({
          frequencyMHz: Number(c.frequencyMHz ?? DEFAULT_GNB.frequencyMHz),
          bandwidthMHz: Number(c.bandwidthMHz ?? DEFAULT_GNB.bandwidthMHz),
          txGainDb: Number(c.txGainDb ?? DEFAULT_GNB.txGainDb),
          rxGainDb: Number(c.rxGainDb ?? DEFAULT_GNB.rxGainDb),
          puschTargetMode: 'manual',
          puschTargetSnrDb: Number(c.puschTargetSnrX10 ?? 89) / 10,
          schedulerMode: c.schedulerMode === 'manual' ? 'manual' : 'auto',
          qm, mcs: Math.min(mcsHigh, Math.max(mcsLow, Number(c.mcs ?? mcsHigh))),
          nPrb: Number(c.nPrb ?? DEFAULT_GNB.nPrb),
        });
        hydrated.current = true;
      }
    }
    const firstFailure = statusResult.status === 'rejected' ? statusResult.reason
      : gnbResult.status === 'rejected' ? gnbResult.reason : undefined;
    setError(firstFailure);
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 1000);
    return () => window.clearInterval(timer);
  }, [load]);

  const stopGnb = async () => {
    setBusy('stop-gnb');
    try {
      const result = await api.post<{ status: GnbStatus }>('/api/diagnostics/rssp-calibration/gnb/service', { action: 'stop' });
      setGnb(result.status); toast('ok', 'gNB stopped.');
    } catch (cause) { setError(cause); toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(''); }
  };

  const configure = async () => {
    setBusy('configure');
    try {
      const payload: Record<string, unknown> = {
        frequencyMHz: gnbForm.frequencyMHz, bandwidthMHz: gnbForm.bandwidthMHz,
        txGainDb: gnbForm.txGainDb, rxGainDb: gnbForm.rxGainDb,
        puschTargetMode: 'manual', puschTargetSnrX10: Math.round(gnbForm.puschTargetSnrDb * 10),
        schedulerMode: gnbForm.schedulerMode,
      };
      if (gnbForm.schedulerMode === 'manual') Object.assign(payload, { qm: gnbForm.qm, mcs: gnbForm.mcs, nPrb: gnbForm.nPrb });
      const result = await api.post<{ status: GnbStatus }>('/api/diagnostics/rssp-calibration/gnb/configure', payload);
      setGnb(result.status); toast('ok', 'gNB configuration applied and restart verified.');
    } catch (cause) { setError(cause); toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(''); }
  };

  const start = async () => {
    setBusy('calibration');
    try {
      const next = await api.post<CalibrationStatus>('/api/diagnostics/rssp-calibration/start', {
        measurement: form.measurement, actuator: form.actuator,
        target_db: Number(form.target_db), tolerance_db: form.tolerance_db,
        gain_alpha: form.gain_alpha,
        max_servo_iters: form.max_servo_iters,
        listening_period_s: form.listening_period_s, settle_time_s: form.settle_time_s,
      });
      setStatus(next); toast('ok', 'Receive-power calibration started.');
    } catch (cause) { setError(cause); toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(''); }
  };

  const stop = async () => {
    setBusy('stop-calibration');
    try {
      setStatus(await api.post<CalibrationStatus>('/api/diagnostics/rssp-calibration/stop'));
      toast('info', 'Stopping calibration and restoring the initial actuator setting.');
    } catch (cause) { setError(cause); toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setBusy(''); }
  };

  const changeMeasurement = (measurement: 'ue_rsrp' | 'pusch_rssi') => setForm({
    ...form, measurement, actuator: measurement === 'ue_rsrp' ? 'tx_gain' : 'target_snr',
    target_db: measurement === 'ue_rsrp' ? '-80' : '-60',
  });
  const stateTone = status.state === 'error' ? 'bad' : terminalGood.has(status.state) ? 'good'
    : terminalWarn.has(status.state) ? 'warn' : status.running ? 'accent' : 'muted';
  const metric = status.metric_label || (form.measurement === 'ue_rsrp' ? 'UE RSRP' : 'PUSCH RSSI');
  const unit = status.unit || (form.measurement === 'ue_rsrp' ? 'dBm' : 'dBFS');
  const telemetryReady = form.measurement === 'ue_rsrp'
    ? gnb?.telemetry?.ueRsrpDbm != null : gnb?.telemetry?.puschRssiDbfs != null;
  const numberField = (label: string, key: keyof GnbConfiguration, unitLabel?: string) => <Field label={label} hint={unitLabel}><input type="number" step={key === 'txGainDb' || key === 'rxGainDb' ? 0.01 : undefined} className="mono" disabled={!!busy || status.running} value={gnbForm[key] as number} onChange={(event) => setGnbForm({ ...gnbForm, [key]: Number(event.target.value) })} /></Field>;
  const [mcsMin, mcsMax] = TABLE2_MCS_RANGE[gnbForm.qm] || TABLE2_MCS_RANGE[8];

  return <div className="stack">
    <div className="page-head"><div><div className="title">RSSP Calibration Diagnostic</div><div className="subtitle">Independent OAI/gNB configuration and receive-power calibration workspace.</div></div><button className="btn" onClick={onBack}>← Advanced</button></div>
    {error ? <ErrorBox error={error} /> : null}{status.error && <div className="error-box">{status.error}</div>}

    <div className="stat-grid">
      <StatCard label="OAI Connection" value={health == null ? 'CHECKING' : health.oai?.ok ? 'CONNECTED' : 'DISCONNECTED'} tone={health == null ? 'muted' : health.oai?.ok ? 'good' : 'bad'} sub={health?.oai?.error || 'Control endpoint'} />
      <StatCard label="gNB State" value={gnb?.gnb?.running ? 'RUNNING' : 'STOPPED'} tone={gnb?.gnb?.running ? 'good' : 'warn'} sub={gnb?.gnb?.status || 'Unknown'} />
      <StatCard label="UE RSRP" value={gnb?.telemetry?.ueRsrpDbm == null ? '—' : `${gnb.telemetry.ueRsrpDbm.toFixed(1)} dBm`} tone={gnb?.telemetry?.ueRsrpDbm == null ? 'muted' : 'good'} sub={`UE reported · ${gnb?.telemetry?.freshUeCount ?? 0} fresh UE(s)`} />
      <StatCard label="PUSCH RSSI" value={gnb?.telemetry?.puschRssiDbfs == null ? '—' : `${gnb.telemetry.puschRssiDbfs.toFixed(1)} dBFS`} tone={gnb?.telemetry?.puschRssiDbfs == null ? 'muted' : 'good'} sub="gNB receiver · live OAI telemetry" />
    </div>

    <Card title="gNB Startup Configuration" sub="Apply the complete OAI radio and uplink configuration with one verified gNB restart.">
      <div className="stack">
        <section><h3>RF</h3><div className="grid cols-4">{numberField('Frequency', 'frequencyMHz', 'MHz')}<Field label="Bandwidth" hint="MHz"><select disabled={!!busy || status.running} value={gnbForm.bandwidthMHz} onChange={(event) => setGnbForm({ ...gnbForm, bandwidthMHz: Number(event.target.value) })}>{[...new Set(gnb?.supportedBandwidthMHz || [20, 40, 100])].map((value) => <option key={value} value={value}>{value} MHz</option>)}</select></Field>{numberField('TX Gain', 'txGainDb', 'dB')}{numberField('RX Gain', 'rxGainDb', 'dB')}</div></section>
        <section><h3>PUSCH Target · Manual</h3><div className="grid cols-2"><Field label="Target SNR" hint="Calculation 0.01 dB · OAI applies 0.1 dB"><div className="row"><input className="grow" type="range" min={0} max={40} step={0.01} disabled={!!busy || status.running} value={gnbForm.puschTargetSnrDb} onChange={(event) => setGnbForm({ ...gnbForm, puschTargetSnrDb: Number(event.target.value) })} /><b className="mono">{gnbForm.puschTargetSnrDb.toFixed(2)} dB</b></div></Field></div></section>
        <section><h3>UL Scheduler</h3><div className="grid cols-4"><Field label="Mode"><select disabled={!!busy || status.running} value={gnbForm.schedulerMode} onChange={(event) => setGnbForm({ ...gnbForm, schedulerMode: event.target.value as 'auto' | 'manual' })}><option value="auto">Auto</option><option value="manual">Manual</option></select></Field>{gnbForm.schedulerMode === 'manual' && <><Field label="Qm" hint="MCS Table 2 only"><select disabled={!!busy || status.running} value={gnbForm.qm} onChange={(event) => { const qm = Number(event.target.value); const [low, high] = TABLE2_MCS_RANGE[qm]; setGnbForm({ ...gnbForm, qm, mcs: Math.min(high, Math.max(low, gnbForm.mcs)) }); }}>{[2, 4, 6, 8].map((qm) => <option key={qm} value={qm}>Qm {qm}</option>)}</select></Field><Field label="MCS" hint={`Table 2 · ${mcsMin}–${mcsMax}`}><div className="row"><input className="grow" type="range" min={mcsMin} max={mcsMax} step={1} disabled={!!busy || status.running} value={gnbForm.mcs} onChange={(event) => setGnbForm({ ...gnbForm, mcs: Number(event.target.value) })} /><b className="mono">{gnbForm.mcs}</b></div></Field>{numberField('N_PRB', 'nPrb')}</>}</div></section>
        <div className="row"><button className="btn primary" disabled={!!busy || status.running} onClick={configure}>{busy === 'configure' ? (gnb?.gnb?.running ? 'Applying and restarting…' : 'Applying and starting…') : (gnb?.gnb?.running ? 'Apply configuration and restart' : 'Apply configuration and start gNB')}</button>{gnb?.gnb?.running && <button className="btn danger" disabled={!!busy || status.running} onClick={stopGnb}>Stop gNB</button>}</div>
      </div>
    </Card>

    <div className="notice-box">UE RSRP is a UE-reported downlink measurement and is calibrated only with gNB TX Gain. PUSCH RSSI is a gNB receiver measurement and can be calibrated with RX Gain or PUSCH Target SNR. Gain changes require a verified gNB restart; Target SNR is applied at runtime. Initial calibration actuator settings are restored when the diagnostic ends.</div>

    <Card title="Receive-Power Calibration Settings" sub="Choose the physical measurement first; the page then exposes only compatible actuators.">
      <div className="grid cols-4">
        <Field label="Measurement"><select disabled={status.running} value={form.measurement} onChange={(event) => changeMeasurement(event.target.value as 'ue_rsrp' | 'pusch_rssi')}><option value="ue_rsrp">UE RSRP (dBm)</option><option value="pusch_rssi">gNB PUSCH RSSI (dBFS)</option></select></Field>
        {form.measurement === 'ue_rsrp' ? <Field label="Calibration actuator"><div className="static-value">TX Gain</div></Field> : <Field label="Calibration actuator"><select disabled={status.running} value={form.actuator} onChange={(event) => setForm({ ...form, actuator: event.target.value as 'rx_gain' | 'target_snr' })}><option value="target_snr">PUSCH Target SNR</option><option value="rx_gain">RX Gain</option></select></Field>}
        <Field label={`Target ${form.measurement === 'ue_rsrp' ? 'RSRP (dBm)' : 'PUSCH RSSI (dBFS)'}`}><input type="text" inputMode="decimal" className="mono" disabled={status.running} value={form.target_db} onChange={(event) => { if (/^-?\d*(\.\d*)?$/.test(event.target.value)) setForm({ ...form, target_db: event.target.value }); }} /></Field>
        <Field label="Tolerance (dB)"><input type="number" min={0.1} max={20} step={0.1} disabled={status.running} value={form.tolerance_db} onChange={(event) => setForm({ ...form, tolerance_db: Number(event.target.value) })} /></Field>
        <Field label="Control α" hint="ΔActuator = α × error"><input type="number" min={0.01} max={1} step={0.01} disabled={status.running} value={form.gain_alpha} onChange={(event) => setForm({ ...form, gain_alpha: Number(event.target.value) })} /></Field>
        <Field label="Max iterations"><input type="number" min={1} max={50} step={1} disabled={status.running} value={form.max_servo_iters} onChange={(event) => setForm({ ...form, max_servo_iters: Number(event.target.value) })} /></Field>
        <Field label="Listening Period (s)" hint="Maximum time without signal"><input type="number" min={1} max={300} step={1} disabled={status.running} value={form.listening_period_s} onChange={(event) => setForm({ ...form, listening_period_s: Number(event.target.value) })} /></Field>
        <Field label="Settle Time (s)" hint="Mean after signal appears"><input type="number" min={0} max={60} step={0.5} disabled={status.running} value={form.settle_time_s} onChange={(event) => setForm({ ...form, settle_time_s: Number(event.target.value) })} /></Field>
      </div>
      <div className="row" style={{ marginTop: 14 }}><button className="btn primary" disabled={!!busy || status.running || !health?.oai?.ok || !gnb?.gnb?.running || !Number.isFinite(Number(form.target_db)) || Number(form.target_db) > 0 || Number(form.target_db) < -140} onClick={start}>Start calibration</button><button className="btn danger" disabled={!!busy || !status.running} onClick={stop}>Stop and restore</button>{!gnb?.gnb?.running && <span className="muted-text">Start gNB before calibration.</span>}{gnb?.gnb?.running && !telemetryReady && <span className="muted-text">Listening for fresh {form.measurement === 'ue_rsrp' ? 'UE RSRP' : 'PUSCH RSSI'} telemetry.</span>}</div>
    </Card>

    <div className="stat-grid">
      <StatCard label={`${metric} / Target`} value={status.last_value_db == null ? '—' : `${status.last_value_db.toFixed(1)} ${unit}`} tone={status.state === 'converged' ? 'good' : 'muted'} sub={`Target ${status.target_db ?? Number(form.target_db)} ${unit}`} />
      <StatCard label="TX / RX Gain" value={`${Number(status.tx_gain_db ?? gnbForm.txGainDb).toFixed(2)} / ${Number(status.rx_gain_db ?? gnbForm.rxGainDb).toFixed(2)} dB`} tone="accent" sub="Current calibration values" />
      <StatCard label="PUSCH Target SNR" value={status.pusch_x10 == null ? `${gnbForm.puschTargetSnrDb.toFixed(2)} dB` : `${(status.pusch_x10 / 10).toFixed(2)} dB`} tone="accent" sub={status.initial_pusch_mode || gnbForm.puschTargetMode} />
      <StatCard label="Calibration / Actuator" value={<Badge tone={stateTone}>{status.state.toUpperCase()}</Badge>} tone={stateTone} sub={`${(status.actuator || form.actuator).replace('_', ' ').toUpperCase()} · ${status.records.length} observation(s)`} />
    </div>

    <Card title="Power Calibration Record" sub="Each point is the mean measured during Settle Time after signal appears."><div style={{ height: 300 }}>{status.records.length ? <ResponsiveContainer><LineChart data={status.records}><XAxis dataKey="iter" label={{ value: 'Iteration', position: 'insideBottomRight' }} allowDecimals={false} /><YAxis unit={` ${unit}`} domain={['auto', 'auto']} /><Tooltip formatter={(value: number | string, name: string, item: { payload?: CalibrationPoint }) => name === metric ? [`${Number(value).toFixed(2)} ${item.payload?.unit} · ${item.payload?.settle_sample_count ?? 0} samples · TX/RX Gain ${item.payload?.tx_gain_db ?? '—'}/${item.payload?.rx_gain_db ?? '—'} dB · Target SNR ${item.payload?.target_snr_db ?? '—'} dB · Error ${item.payload?.err_db ?? '—'} dB`, `Iteration ${item.payload?.iter ?? '—'}`] : [value, name]} /><Legend /><Line dataKey="observed_db" name={metric} stroke="#3157d5" strokeWidth={2} dot={{ r: 5 }} isAnimationActive={false} /><ReferenceLine y={status.target_db ?? Number(form.target_db)} stroke="#c0392b" strokeDasharray="6 4" label={`Target ${metric}`} /></LineChart></ResponsiveContainer> : <div className="muted-text">Calibration points will appear after OAI returns the first fresh measurement.</div>}</div></Card>

    <Card title="Calibration Observations" sub="Every observation records all three possible control variables, while only the selected actuator changes."><div className="table-wrap"><table className="data"><thead><tr><th>Iteration</th><th>Measurement</th><th>Observed</th><th>Target</th><th>Error</th><th>TX / RX Gain</th><th>Target SNR</th><th>Applied</th></tr></thead><tbody>{status.records.length ? status.records.map((point) => <tr key={`${point.ms}-${point.iter}`}><td>{point.iter}</td><td>{point.metric_label}</td><td>{point.observed_db.toFixed(2)} {point.unit}</td><td>{point.target_db.toFixed(2)} {point.unit}</td><td>{point.err_db > 0 ? '+' : ''}{point.err_db.toFixed(2)} dB</td><td>{point.tx_gain_db?.toFixed(2) ?? '—'} / {point.rx_gain_db?.toFixed(2) ?? '—'} dB</td><td>{point.target_snr_db?.toFixed(2) ?? '—'} dB</td><td>{point.applied_actuator ? `${point.applied_actuator.replace('_', ' ')} → ${Number(point.applied_value).toFixed(2)} ${point.applied_unit}` : '—'}</td></tr>) : <tr><td colSpan={8}>No observations yet.</td></tr>}</tbody></table></div></Card>

    <Card title="Diagnostic Log" sub="OAI connection, gNB restarts, calibration decisions and restore outcome."><div className="table-wrap"><table className="data"><thead><tr><th>Time</th><th>Stage</th><th>Message</th></tr></thead><tbody>{status.log.length ? [...status.log].reverse().map((row, index) => <tr key={`${row.ms}-${index}`}><td>{new Date(row.ms).toLocaleTimeString()}</td><td>{row.stage}</td><td>{row.msg}</td></tr>) : <tr><td colSpan={3}>No diagnostic activity yet.</td></tr>}</tbody></table></div></Card>
  </div>;
}
