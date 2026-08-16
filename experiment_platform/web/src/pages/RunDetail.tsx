import { useMemo, useState } from 'react';
import {
  Bar,
  Brush,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  LineChart,
  ReferenceArea,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { api, downloadFile } from '../api';
import type { MergedRow, Run } from '../types';
import { Badge, Card, EmptyState, ErrorBox, Spinner } from '../components/ui';
import { useLoad } from '../components/DataView';
import { fmtDurationMs, fmtIso } from '../format';

const SYNC_ID = 'run-detail';

const PHASE_COLORS: Record<string, string> = {
  BASELINE: '#dfe8f2',
  ACTIVE: '#fbe3bb',
  TAIL: '#d7eedb',
};

interface SeriesDef {
  key: string;
  label: string;
  color: string;
  step?: boolean;
}

interface PanelDef {
  id: string;
  title: string;
  unit: string;
  kind: 'line' | 'tpc' | 'ratio';
  series: SeriesDef[];
}

const PANELS: PanelDef[] = [
  { id: 'power', title: 'Phone · battery power', unit: 'W', kind: 'line', series: [{ key: 'phone_power_w_mean', label: 'power (mean)', color: '#3b5bdb' }] },
  { id: 'current', title: 'Phone · battery current', unit: 'µA', kind: 'line', series: [
    { key: 'phone_current_ua_mean', label: 'current mean', color: '#3b5bdb' },
    { key: 'phone_current_ua_median', label: 'current median', color: '#8aa4e8' },
  ] },
  { id: 'voltage', title: 'Phone · voltage', unit: 'mV', kind: 'line', series: [{ key: 'phone_voltage_mv_mean', label: 'voltage', color: '#7c4bd8' }] },
  { id: 'rsrp', title: 'Phone · SS-RSRP', unit: 'dBm', kind: 'line', series: [
    { key: 'phone_rsrp_dbm_median', label: 'RSRP median', color: '#d96a1f' },
    { key: 'phone_rsrp_dbm_p10', label: 'RSRP p10', color: '#e6a06f' },
    { key: 'phone_rsrp_dbm_p90', label: 'RSRP p90', color: '#e6a06f' },
  ] },
  { id: 'sinr', title: 'Phone · SS-SINR', unit: 'dB', kind: 'line', series: [{ key: 'phone_sinr_db_median', label: 'SINR median', color: '#0f9d76' }] },
  { id: 'temp', title: 'Phone · temperature', unit: '°C', kind: 'line', series: [{ key: 'phone_temperature_c_mean', label: 'temperature', color: '#d96a1f' }] },
  { id: 'pusch', title: 'gNB · PUSCH SNR', unit: 'dB', kind: 'line', series: [
    { key: 'gnb_pusch_snr_db', label: 'PUSCH SNR (snapshot)', color: '#0e7c5f' },
    { key: 'pusch_snr_mean', label: 'PUSCH SNR mean (events)', color: '#4fb598' },
  ] },
  { id: 'ph', title: 'gNB · power headroom', unit: 'dB', kind: 'line', series: [
    { key: 'gnb_ph_raw_db', label: 'PH raw', color: '#a34fc4' },
    { key: 'gnb_ph_normalized_db', label: 'PH normalized', color: '#4f8ab3' },
    { key: 'ph_normalized_mean', label: 'PH norm (events)', color: '#9ec4e0' },
  ] },
  { id: 'pcmax', title: 'gNB · PCMAX', unit: 'dBm', kind: 'line', series: [{ key: 'gnb_pcmax_dbm', label: 'PCMAX', color: '#a97a42' }] },
  { id: 'tpc', title: 'gNB · TPC (per-second counts)', unit: 'count', kind: 'tpc', series: [
    { key: 'tpc_positive_count', label: 'TPC +', color: '#1a9d54' },
    { key: 'tpc_zero_count', label: 'TPC 0', color: '#a7b1bd' },
    { key: 'tpc_negative_count', label: 'TPC −', color: '#d93025' },
  ] },
  { id: 'tpc_ratio', title: 'gNB · TPC positive ratio', unit: 'ratio', kind: 'ratio', series: [{ key: 'tpc_positive_ratio', label: 'TPC + / total', color: '#1a9d54' }] },
  { id: 'mcs', title: 'gNB · MCS', unit: 'index', kind: 'line', series: [
    { key: 'gnb_ul_mcs', label: 'UL MCS', color: '#3b5bdb', step: true },
    { key: 'ul_mcs_mode', label: 'UL MCS (events)', color: '#8aa4e8', step: true },
    { key: 'gnb_dl_mcs', label: 'DL MCS', color: '#e08a2e', step: true },
  ] },
  { id: 'nprb', title: 'gNB · N_PRB', unit: 'PRB', kind: 'line', series: [
    { key: 'gnb_n_prb', label: 'N_PRB', color: '#7c4bd8', step: true },
    { key: 'n_prb_mean', label: 'N_PRB (events)', color: '#b39ae8', step: true },
  ] },
  { id: 'bler', title: 'gNB · BLER', unit: 'ratio', kind: 'line', series: [
    { key: 'gnb_ul_bler', label: 'UL BLER', color: '#d93025' },
    { key: 'gnb_dl_bler', label: 'DL BLER', color: '#e08a2e' },
  ] },
  { id: 'harq', title: 'gNB · HARQ retransmission ratio', unit: 'ratio', kind: 'ratio', series: [{ key: 'gnb_harq_retransmission_ratio', label: 'HARQ retx ratio', color: '#a34fc4' }] },
  { id: 'goodput', title: 'gNB · goodput', unit: 'Mbps', kind: 'line', series: [
    { key: 'gnb_ul_goodput_mbps', label: 'UL goodput', color: '#0e7c5f' },
    { key: 'gnb_dl_goodput_mbps', label: 'DL goodput', color: '#3b5bdb' },
  ] },
];

function phaseBands(rows: MergedRow[]): { phase: string; x1: number; x2: number }[] {
  const bands: { phase: string; x1: number; x2: number }[] = [];
  if (rows.length === 0) return bands;
  let cur = rows[0].phase ?? '';
  let x1 = rows[0].window_ms;
  for (let i = 1; i < rows.length; i++) {
    const p = rows[i].phase ?? '';
    if (p !== cur) {
      bands.push({ phase: cur, x1, x2: rows[i].window_ms });
      cur = p;
      x1 = rows[i].window_ms;
    }
  }
  bands.push({ phase: cur, x1, x2: rows[rows.length - 1].window_ms + 1000 });
  return bands;
}

function compact(v: number): string {
  if (Math.abs(v) >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (Math.abs(v) >= 1e3) return `${(v / 1e3).toFixed(1)}k`;
  return String(v);
}

function relLabel(v: number, t0: number): string {
  return `${((v - t0) / 1000).toFixed(0)}s`;
}

export default function RunDetail({ runId, nav }: { runId?: string; nav: (p: string) => void }) {
  const [inputId, setInputId] = useState(runId ?? '');
  const activeId = runId ?? inputId;

  const runLoad = useLoad<Run>(
    () => (activeId ? api.get(`/api/runs/${encodeURIComponent(activeId)}`) : Promise.reject(new Error('no run id'))),
    [activeId],
  );
  const mergedLoad = useLoad<MergedRow[]>(
    () => (activeId ? api.get(`/api/runs/${encodeURIComponent(activeId)}/merged`) : Promise.reject(new Error('no run id'))),
    [activeId],
  );

  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const [zoom, setZoom] = useState<{ start: number; end: number } | null>(null);
  const [opBusy, setOpBusy] = useState<string | null>(null);
  const [opMsg, setOpMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const rows = mergedLoad.data ?? [];
  const t0 = rows.length ? rows[0].window_ms : 0;
  const bands = useMemo(() => phaseBands(rows), [rows]);

  const xDomain: [number | string, number | string] = zoom
    ? [rows[zoom.start]?.window_ms ?? 0, rows[zoom.end]?.window_ms ?? 0]
    : ['dataMin', 'dataMax'];

  const toggleSeries = (key: string) => {
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const runOp = async (op: 'align' | 'quality' | 'collect/start' | 'collect/stop') => {
    if (!activeId) return;
    setOpBusy(op);
    setOpMsg(null);
    try {
      const r = await api.post<unknown>(`/api/runs/${encodeURIComponent(activeId)}/${op}`);
      setOpMsg({ kind: 'ok', text: `${op} OK — ${JSON.stringify(r)}` });
      runLoad.reload();
      if (op === 'align') mergedLoad.reload();
    } catch (e) {
      setOpMsg({ kind: 'err', text: e instanceof Error ? e.message : String(e) });
    } finally {
      setOpBusy(null);
    }
  };

  const renderChart = (panel: PanelDef) => {
    const visible = panel.series.filter((s) => !hidden.has(s.key));
    const yDomain: [number | string, number | string] = panel.kind === 'ratio' ? [0, 1] : panel.kind === 'tpc' ? [0, 'auto'] : ['auto', 'auto'];
    const common = (
      <>
        <CartesianGrid strokeDasharray="3 3" stroke="#e8edf4" />
        <XAxis
          dataKey="window_ms"
          type="number"
          domain={xDomain}
          tickFormatter={(v: number) => relLabel(v, t0)}
          tick={{ fontSize: 10, fontFamily: 'monospace' }}
          stroke="#cbd4e2"
          allowDataOverflow
        />
        <YAxis domain={yDomain} tick={{ fontSize: 10, fontFamily: 'monospace' }} tickFormatter={compact} width={52} stroke="#cbd4e2" allowDataOverflow />
        <Tooltip
          labelFormatter={(l) => relLabel(Number(l), t0)}
          formatter={(value: unknown) => [typeof value === 'number' ? value.toFixed(3) : String(value)]}
          contentStyle={{ fontFamily: 'monospace', fontSize: 12, borderRadius: 8, border: '1px solid #e5e9f2' }}
        />
        <Legend wrapperStyle={{ fontSize: 11 }} />
        {bands
          .filter((b) => PHASE_COLORS[b.phase])
          .map((b, i) => (
            <ReferenceArea key={i} x1={b.x1} x2={b.x2} fill={PHASE_COLORS[b.phase]} fillOpacity={0.55} stroke="none" />
          ))}
      </>
    );

    if (panel.kind === 'tpc') {
      return (
        <ComposedChart data={rows} syncId={SYNC_ID}>
          {common}
          {panel.series.map(
            (s) =>
              !hidden.has(s.key) && (
                <Bar key={s.key} dataKey={s.key} name={s.label} stackId="tpc" fill={s.color} isAnimationActive={false} maxBarSize={12} />
              ),
          )}
        </ComposedChart>
      );
    }

    return (
      <LineChart data={rows} syncId={SYNC_ID}>
        {common}
        {panel.series.map(
          (s) =>
            !hidden.has(s.key) && (
              <Line
                key={s.key}
                type={s.step ? 'stepAfter' : 'monotone'}
                dataKey={s.key}
                name={s.label}
                stroke={s.color}
                dot={false}
                isAnimationActive={false}
                connectNulls
                strokeWidth={1.5}
              />
            ),
        )}
      </LineChart>
    );
  };

  const run = runLoad.data;

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Run Detail</div>
          <div className="subtitle">inspect and control a single run</div>
        </div>
        <div className="row gap-sm">
          <input
            className="mono"
            style={{ width: 180 }}
            value={inputId}
            placeholder="e.g. R001"
            onChange={(e) => setInputId(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && inputId) nav(`/run/${encodeURIComponent(inputId)}`);
            }}
          />
          <button className="btn" disabled={!inputId} onClick={() => nav(`/run/${encodeURIComponent(inputId)}`)}>
            Load
          </button>
          {run && <button className="btn ghost" onClick={() => nav(`/experiments`)}>← Experiments</button>}
        </div>
      </div>

      <Card title="Run metadata">
        {runLoad.loading && <Spinner label="loading run…" />}
        {runLoad.error && <ErrorBox error={runLoad.error} />}

        {run && (
          <>
            <div className="row gap-sm" style={{ marginBottom: 12 }}>
              <Badge tone="accent">{run.run_id}</Badge>
              <Badge tone={run.state === 'FAILED' ? 'bad' : run.state === 'WARNING' ? 'warn' : run.state === 'COMPLETE' ? 'good' : 'muted'}>
                {run.state}
              </Badge>
              {run.quality_status && (
                <Badge tone={run.quality_status === 'PASS' ? 'good' : run.quality_status === 'WARNING' ? 'warn' : 'bad'}>
                  quality {run.quality_status}
                </Badge>
              )}
              <Badge tone="muted">{run.experiment_id}</Badge>
              <Badge tone="muted">{run.condition_id}</Badge>
              {run.device_id && <Badge tone="muted">device {run.device_id}</Badge>}
            </div>

            <div className="kv" style={{ marginBottom: 12 }}>
              <dt>environment</dt><dd>{run.condition?.environment ?? '—'}</dd>
              <dt>started</dt><dd>{fmtIso(run.started_utc_ms ? new Date(run.started_utc_ms).toISOString() : undefined)}</dd>
              <dt>ended</dt><dd>{fmtIso(run.ended_utc_ms ? new Date(run.ended_utc_ms).toISOString() : undefined)}</dd>
              <dt>start_delay_s</dt><dd>{run.start_delay_s ?? '—'}</dd>
              <dt>quality flags</dt>
              <dd>{run.quality_flags && run.quality_flags.length ? run.quality_flags.join(', ') : '—'}</dd>
              {run.state === 'FAILED' && run.last_error ? (
                <>
                  <dt>error</dt>
                  <dd style={{ color: 'var(--bad)' }}>{String(run.last_error)}</dd>
                </>
              ) : null}
            </div>

            <div className="row gap-sm" style={{ marginBottom: 12 }}>
              <button className="btn" disabled={opBusy === 'collect/start'} onClick={() => runOp('collect/start')}>
                {opBusy === 'collect/start' ? '…' : 'Collect start'}
              </button>
              <button className="btn" disabled={opBusy === 'collect/stop'} onClick={() => runOp('collect/stop')}>
                {opBusy === 'collect/stop' ? '…' : 'Collect stop'}
              </button>
              <button className="btn" disabled={opBusy === 'align'} onClick={() => runOp('align')}>
                {opBusy === 'align' ? '…' : 'Align & merge'}
              </button>
              <button className="btn" disabled={opBusy === 'quality'} onClick={() => runOp('quality')}>
                {opBusy === 'quality' ? '…' : 'Run quality'}
              </button>
              <button className="btn" onClick={() => downloadFile(`/api/runs/${encodeURIComponent(run.run_id)}/merged.csv`, `${run.run_id}_merged_1s.csv`)}>
                merged.csv ↓
              </button>
            </div>

            {opMsg && (
              <div className={opMsg.kind === 'err' ? 'error-box' : 'notice-box'}>
                {opMsg.text}
              </div>
            )}
          </>
        )}
      </Card>

      {mergedLoad.error && (
        <Card title="Merged data">
          <ErrorBox error={mergedLoad.error} />
          <div className="notice-box" style={{ marginTop: 8 }}>
            No merged 1s data for this run. Import phone data, then click <b>Align &amp; merge</b> above.
          </div>
        </Card>
      )}

      {mergedLoad.loading && !mergedLoad.error && <Spinner label="loading merged data…" />}

      {!mergedLoad.error && rows.length > 0 && (
        <>
          <details className="details" open>
            <summary>Series toggle & phase legend</summary>
            <div className="details-body">
              <div className="stack">
                <div className="phase-legend">
                  <span><span className="phase-swatch" style={{ background: PHASE_COLORS.BASELINE }} />BASELINE</span>
                  <span><span className="phase-swatch" style={{ background: PHASE_COLORS.ACTIVE }} />ACTIVE</span>
                  <span><span className="phase-swatch" style={{ background: PHASE_COLORS.TAIL }} />TAIL</span>
                  <span className="badge muted">{rows.length} windows</span>
                  <span className="badge muted">{fmtDurationMs(rows[rows.length - 1].window_ms - rows[0].window_ms)}</span>
                  {zoom && <button className="btn sm" onClick={() => setZoom(null)}>reset zoom</button>}
                </div>
                <div className="series-toggle">
                  {PANELS.flatMap((p) => p.series).map((s) => (
                    <label key={s.key}>
                      <input type="checkbox" checked={!hidden.has(s.key)} onChange={() => toggleSeries(s.key)} />
                      <span className="swatch" style={{ background: s.color }} />
                      {s.label}
                    </label>
                  ))}
                </div>
              </div>
            </div>
          </details>

          {PANELS.map((p) => {
            const anyVisible = p.series.some((s) => !hidden.has(s.key));
            return (
              <div className="chart-panel" key={p.id}>
                <div className="chart-head">
                  <h4>{p.title}</h4>
                  <span className="unit">{p.unit}</span>
                </div>
                <div style={{ height: 150 }}>
                  {anyVisible ? (
                    <ResponsiveContainer width="100%" height="100%">
                      {renderChart(p)}
                    </ResponsiveContainer>
                  ) : (
                    <div className="notice-box">all series hidden</div>
                  )}
                </div>
              </div>
            );
          })}

          <div className="chart-panel">
            <div className="chart-head">
              <h4>zoom / brush (shared time axis)</h4>
              <span className="unit">drag to zoom — controls all charts above</span>
            </div>
            <div style={{ height: 90 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={rows}>
                  <XAxis dataKey="window_ms" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10, fontFamily: 'monospace' }} stroke="#cbd4e2" />
                  <YAxis hide />
                  <Line dataKey="phone_power_w_mean" dot={false} isAnimationActive={false} stroke="#3b5bdb" />
                  <Brush
                    dataKey="window_ms"
                    height={24}
                    stroke="#3b5bdb"
                    travellerWidth={8}
                    onChange={(e: { startIndex?: number; endIndex?: number }) => {
                      if (e.startIndex !== undefined && e.endIndex !== undefined) {
                        setZoom({ start: e.startIndex, end: e.endIndex });
                      }
                    }}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        </>
      )}

      {!mergedLoad.error && !mergedLoad.loading && rows.length === 0 && !runLoad.loading && (
        <Card title="Merged data">
          <EmptyState>No merged rows available.</EmptyState>
        </Card>
      )}
    </div>
  );
}
