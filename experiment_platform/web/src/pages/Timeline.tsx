import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { Badge, Card, EmptyState, ErrorBox, Field, Modal, Spinner, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { useOperatorContext } from '../context';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine, ReferenceArea, ReferenceDot } from 'recharts';

type Run = { run_id: string; experiment_id?: string; state: string; started_utc_ms: number | null; ended_utc_ms: number | null; configuration_id?: number; configuration_version?: number; configuration_name?: string; configuration_snapshot_json?: string | null; execution_mode?: string; simulation?: number; quality_status?: string };
type Phone = { utc_epoch_ms: number | null; battery_power_w: number | null; ss_rsrp_dbm: number | null };
type Gnb = { fetched_utc_ms: number; ul_goodput_mbps: number | null; dl_goodput_mbps: number | null; ul_bler: number | null; dl_bler: number | null; ul_harq_retransmission_ratio: number | null; dl_harq_retransmission_ratio: number | null };
type Channel = { fetched_utc_ms: number; rms_delay_ns: number | null; k_factor_db: number | null; tap_count: number | null };
type FrequencyPoint = { frequency_mhz: number; magnitude_db: number; phase_deg: number; real: number; imag: number };
type Path = { path: number; path_id?: string; delay_ns: number; excess_delay_ns?: number; power_db: number; relative_power_db?: number; phase_deg?: number; phase_calibration?: string; peak_index: number; margin_above_threshold_db?: number; peak_prominence_db?: number; confidence?: string; near_threshold?: boolean; is_first_detected?: boolean; is_strongest?: boolean };
type ChannelAnalysis = { processing_status: string; processing_error?: string | null; noise_floor_db?: number; noise_margin_db?: number; detection_threshold_db?: number; raw_delay_bin_count?: number; candidate_peak_count?: number; effective_peak_count?: number; resolved_path_count?: number; effective_path_count?: number; rms_delay_ns_filtered?: number; k_factor_db_filtered?: number; resolved_paths?: Path[]; effective_paths?: Path[]; processing_version?: string; processing_algorithm?: string; delay_reference?: string; effective_bandwidth_mhz?: number; nominal_delay_resolution_ns?: number; minimum_resolvable_separation_ns?: number; capture_delay_window_ns?: { start_ns: number; end_ns: number }; analysis_delay_window_ns?: { start_ns: number; end_ns: number; source: string } | null; peak_prominence_threshold_db?: number; peak_detection_method?: string; window_function?: string; frequency_spacing_mhz?: number; frequency_grid_consistency?: string; frequency_response_source?: string; frequency_response?: FrequencyPoint[]; calibration?: Record<string, string> };
type Analytics = { identity: { sample_id: number; angle_deg: number }; window: { start_utc_ms: number; end_utc_ms: number }; channel: ChannelAnalysis; radio: { rssp_dbfs?: number; target_rssp_dbfs?: number; pusch_target_snr_x10?: number }; link: Record<string, number | string | null>; quality: { data_complete: boolean; processing_status: string; alignment_status?: string } };
type RcSample = { id: number; sample_index: number; run_id: string; started_utc_ms: number; ended_utc_ms: number; stirrer_angle_deg: number; analytics?: Analytics; pdp?: { tau_ns: number; power_db: number }[]; pdp_dt_ns?: number; display_delay_window_ns?: { start_ns: number; end_ns: number; source: string } };
type SegmentRow = { id?: number; source_run_id: string; source_start_relative_ms: number; source_end_relative_ms: number; segment_order?: number; label: string };
type Clip = { id: number; run_id: string; label: string; created_utc: string; updated_utc?: string; segments: SegmentRow[] };
type TimelineEvent = { timestamp_utc_ms: number; label: string; kind: string };
type TimelineData = { environment: 'AC' | 'RC'; t0_utc_ms: number | null; t0_source: string | null; master: { start_utc_ms: number | null; end_utc_ms: number | null }; alignment: Record<string, { status: string; count: number; first_utc_ms?: number | null; last_utc_ms?: number | null; raw_offset_ms: number | null; applied_correction_ms: number }>; runs: Run[]; samples: Phone[]; gnb: Gnb[]; channel: Channel[]; rc_samples: RcSample[]; clips: Clip[]; events: TimelineEvent[]; cir?: { pdp: { tau_ns: number; power_db: number }[] } | null };
type Segment = { key: number; start: number; end: number; label: string };

const COLORS = ['#3b5bdb', '#2e9e5b', '#e67e22', '#8e44ad', '#c0392b'];
const EVENT_COLORS: Record<string, string> = {
  PREPARING: '#3b5bdb', ARMED: '#d97706', RUNNING: '#159447', STOPPED: '#64748b', ERROR: '#d93025', FAILED: '#d93025',
  RC_SAMPLE_START: '#8e44ad', RC_SAMPLE_COMPLETE: '#0f8f8f',
};
const eventCategory = (event: TimelineEvent) => event.kind === 'RUN_STATE' ? event.label.toUpperCase() : event.kind.toUpperCase();
const eventColor = (event: TimelineEvent) => EVENT_COLORS[eventCategory(event)] || '#52627a';
const relativeTime = (value: number) => `T${value < 0 ? '−' : '+'}${Math.abs(value).toFixed(3)}s`;
const percent = (fraction: unknown) => typeof fraction === 'number' ? `${(fraction * 100).toFixed(1)}%` : 'Unavailable';
const number = (value: unknown, digits = 1, unit = '') => typeof value === 'number' ? `${value.toFixed(digits)}${unit}` : 'Unavailable';
function addGaps<T extends { t: number }>(rows: T[], keys: string[]): Array<T | Record<string, unknown>> {
  if (rows.length < 3) return rows;
  const intervals = rows.slice(1).map((row, index) => row.t - rows[index].t).filter((value) => value > 0).sort((a, b) => a - b);
  const expected = intervals[Math.floor(intervals.length / 2)] || 1;
  const out: Array<T | Record<string, unknown>> = [];
  rows.forEach((row, index) => {
    if (index && row.t - rows[index - 1].t > expected * 2.5) out.push({ t: rows[index - 1].t + expected, ...Object.fromEntries(keys.map((key) => [key, null])) });
    out.push(row);
  });
  return out;
}

function Track({ title, sub, data, domain, selection, playhead, lines, onPlayhead }: { title: string; sub: string; data: any[]; domain: [number, number]; selection: [number, number]; playhead: number; lines: { key: string; label: string; color: string; axis?: string }[]; onPlayhead: (value: number) => void }) {
  return <Card title={title} sub={sub}>{data.length ? <div style={{ height: 160 }}><ResponsiveContainer><LineChart data={data} onClick={(state: any) => typeof state?.activeLabel === 'number' && onPlayhead(state.activeLabel)}><XAxis dataKey="t" type="number" domain={domain} allowDataOverflow tick={{ fontSize: 10 }} /><YAxis yAxisId="left" tick={{ fontSize: 10 }} /><YAxis yAxisId="right" orientation="right" tick={{ fontSize: 10 }} /><Tooltip labelFormatter={(value) => `T+${Number(value).toFixed(3)}s`} /><ReferenceArea yAxisId="left" x1={selection[0]} x2={selection[1]} fill="#3b5bdb" fillOpacity={0.12} /><ReferenceLine yAxisId="left" x={playhead} stroke="#151922" />{lines.map((line) => <Line key={line.key} yAxisId={line.axis || 'left'} dataKey={line.key} name={line.label} dot={false} isAnimationActive={false} connectNulls={false} stroke={line.color} strokeWidth={2} />)}</LineChart></ResponsiveContainer></div> : <EmptyState>Unavailable for this Run.</EmptyState>}</Card>;
}

function measuredDelayWindow(samples: RcSample[]) {
  const recorded = samples.map((sample) => sample.analytics?.channel.analysis_delay_window_ns).filter((window): window is NonNullable<typeof window> => !!window && window.end_ns > window.start_ns);
  if (recorded.length) return { start: Math.min(...recorded.map((window) => window.start_ns)), end: Math.max(...recorded.map((window) => window.end_ns)), source: recorded.some((window) => window.source.startsWith('CONFIGURED')) ? 'Configured chamber/system window' : 'Auto resolved-path envelope' };
  const display = samples.map((sample) => sample.display_delay_window_ns).filter((window): window is NonNullable<typeof window> => !!window && window.end_ns > window.start_ns);
  if (display.length) return { start: Math.min(...display.map((window) => window.start_ns)), end: Math.max(...display.map((window) => window.end_ns)), source: display[0].source.split('_').join(' ') };
  const extents = samples.flatMap((sample) => {
    const pdp = sample.pdp || [];
    if (!pdp.length) return [];
    const peak = Math.max(...pdp.map((point) => point.power_db));
    const threshold = sample.analytics?.channel.detection_threshold_db ?? peak - 30;
    const captureEnd = pdp[pdp.length - 1].tau_ns;
    const significant = pdp.filter((point) => point.tau_ns <= captureEnd / 2 && point.power_db >= threshold);
    return significant.length ? [significant[significant.length - 1].tau_ns] : [];
  });
  const captureEnd = Math.max(...samples.flatMap((sample) => (sample.pdp || []).slice(-1).map((point) => point.tau_ns)), 1);
  if (!extents.length) return { start: 0, end: captureEnd, source: 'Full capture · physical window unavailable' };
  const measuredEnd = Math.max(...extents);
  const dt = Math.min(...samples.flatMap((sample) => sample.pdp_dt_ns ? [sample.pdp_dt_ns] : []), 10);
  const padded = Math.min(captureEnd / 2, measuredEnd + Math.max(4 * dt, measuredEnd * 0.15));
  const step = Math.max(10, 10 ** Math.floor(Math.log10(Math.max(padded, 1))) / 2);
  return { start: 0, end: Math.ceil(padded / step) * step, source: 'Auto measured-energy envelope · threshold, or peak −30 dB fallback' };
}

function PdpHeatmap({ samples, selectedId, onSelect }: { samples: RcSample[]; selectedId: number | null; onSelect: (sample: RcSample) => void }) {
  const [mode, setMode] = useState<'filtered' | 'raw' | 'overlay'>('filtered');
  const [windowMode, setWindowMode] = useState<'physical' | 'full'>('physical');
  const physicalWindow = measuredDelayWindow(samples);
  const fullEnd = Math.max(...samples.flatMap((sample) => (sample.pdp || []).slice(-1).map((point) => point.tau_ns)), 1);
  const delayWindow = windowMode === 'physical' ? physicalWindow : { start: 0, end: fullEnd, source: 'Full captured delay span' };
  const powers = samples.flatMap((sample) => (sample.pdp || []).filter((point) => point.tau_ns >= delayWindow.start && point.tau_ns <= delayWindow.end).map((point) => point.power_db)).sort((a, b) => a - b);
  if (!powers.length) return <EmptyState>PDP sequence unavailable.</EmptyState>;
  const scalePowers = powers.filter((power) => power > -250);
  const scale = scalePowers.length ? scalePowers : powers;
  const low = Math.floor(scale[Math.floor((scale.length - 1) * 0.05)]);
  const high = Math.ceil(scale[Math.floor((scale.length - 1) * 0.99)]);
  const rowHeight = 30;
  const chartWidth = 1000;
  const labelWidth = 115;
  const plotRight = 850;
  const colorbarX = 900;
  const color = (power: number) => {
    const value = Math.max(0, Math.min(1, (power - low) / Math.max(1, high - low)));
    return `hsl(${245 - value * 245} 82% ${24 + value * 34}%)`;
  };
  return <div className="stack compact">
    <div className="row between"><div><b>Delay window {delayWindow.start.toFixed(0)}–{delayWindow.end.toFixed(0)} ns</b><div className="muted-text">{delayWindow.source}</div></div><div className="row"><div className="segmented"><button className={windowMode === 'physical' ? 'active' : ''} onClick={() => setWindowMode('physical')}>Physical window</button><button className={windowMode === 'full' ? 'active' : ''} onClick={() => setWindowMode('full')}>Full capture</button></div><div className="segmented">{(['filtered', 'raw', 'overlay'] as const).map((item) => <button key={item} className={mode === item ? 'active' : ''} onClick={() => setMode(item)}>{item === 'filtered' ? 'Filtered' : item === 'raw' ? 'Raw' : 'Overlay'}</button>)}</div></div></div>
    <div style={{ overflowX: 'auto' }}><svg role="img" aria-label="PDP heatmap by RC sample" viewBox={`0 0 ${chartWidth} ${samples.length * rowHeight + 28}`} style={{ width: '100%', minWidth: 720 }}>
      <defs><linearGradient id="pdp-power-colorbar" x1="0" y1="1" x2="0" y2="0"><stop offset="0%" stopColor="hsl(245 82% 24%)" /><stop offset="50%" stopColor="hsl(122 82% 41%)" /><stop offset="100%" stopColor="hsl(0 82% 58%)" /></linearGradient></defs>
      {samples.map((sample, row) => {
        const pdp = (sample.pdp || []).filter((point) => point.tau_ns >= delayWindow.start && point.tau_ns <= delayWindow.end);
        const stride = Math.max(1, Math.floor(pdp.length / 240));
        const threshold = sample.analytics?.channel.detection_threshold_db;
        const paths = (sample.analytics?.channel.resolved_paths || sample.analytics?.channel.effective_paths || []).filter((path) => path.delay_ns >= delayWindow.start && path.delay_ns <= delayWindow.end);
        return <g key={sample.id} onClick={() => onSelect(sample)} style={{ cursor: 'pointer' }}>
          {sample.id === selectedId && <rect x="0" y={row * rowHeight} width={chartWidth} height={rowHeight} fill="#3b5bdb" opacity="0.10" />}
          <text x="4" y={row * rowHeight + 19} fontSize="11" fill="currentColor">S{sample.sample_index} · {sample.stirrer_angle_deg.toFixed(1)}°</text>
          {pdp.filter((_, index) => index % stride === 0).map((point, index, shown) => {
            const next = shown[index + 1];
            const x = labelWidth + (point.tau_ns - delayWindow.start) / (delayWindow.end - delayWindow.start) * (plotRight - labelWidth);
            const width = Math.max(1, ((next?.tau_ns ?? delayWindow.end) - point.tau_ns) / (delayWindow.end - delayWindow.start) * (plotRight - labelWidth));
            const below = threshold != null && point.power_db < threshold;
            return <rect key={point.tau_ns} x={x} y={row * rowHeight + 4} width={width + 0.5} height={rowHeight - 8} fill={color(point.power_db)} opacity={mode === 'raw' ? 1 : below ? 0.13 : 1}><title>Sample {sample.sample_index} · {point.tau_ns.toFixed(1)} ns · Raw PDP {point.power_db.toFixed(1)} dB{threshold == null ? '' : ` · ${point.power_db >= threshold ? 'above' : 'below'} threshold`}</title></rect>;
          })}
          {mode !== 'raw' && paths.map((path) => { const x = labelWidth + (path.delay_ns - delayWindow.start) / (delayWindow.end - delayWindow.start) * (plotRight - labelWidth); const y = row * rowHeight + rowHeight / 2; return <g key={path.path}><title>{path.path_id || `P${path.path}`} · Delay {path.delay_ns.toFixed(1)} ns · Power {path.power_db.toFixed(1)} dB · Relative {number(path.relative_power_db, 1, ' dB')} · Above threshold {number(path.margin_above_threshold_db, 1, ' dB')} · Phase {number(path.phase_deg, 1, '°')} · Confidence {path.confidence || 'Unknown'}</title><line x1={x - 7} y1={y} x2={x + 7} y2={y} stroke="#fff" strokeWidth="4" /><line x1={x} y1={y - 7} x2={x} y2={y + 7} stroke="#fff" strokeWidth="4" /><circle cx={x} cy={y} r="5" fill="#ff2d55" stroke="#151922" strokeWidth="1.5" /></g>; })}
        </g>;
      })}
      {[0, 0.25, 0.5, 0.75, 1].map((fraction) => <text key={fraction} x={labelWidth + fraction * (plotRight - labelWidth)} y={samples.length * rowHeight + 22} textAnchor={fraction === 0 ? 'start' : fraction === 1 ? 'end' : 'middle'} fontSize="11">{(delayWindow.start + fraction * (delayWindow.end - delayWindow.start)).toFixed(0)} ns</text>)}
      <rect x={colorbarX} y="8" width="18" height={Math.max(36, samples.length * rowHeight - 12)} fill="url(#pdp-power-colorbar)" /><text x={colorbarX - 4} y="12" textAnchor="end" fontSize="10">{high} dB</text><text x={colorbarX - 4} y={Math.max(36, samples.length * rowHeight - 12) / 2 + 8} textAnchor="end" fontSize="10">{((high + low) / 2).toFixed(0)} dB</text><text x={colorbarX - 4} y={Math.max(36, samples.length * rowHeight - 4)} textAnchor="end" fontSize="10">{low} dB</text><text x={colorbarX + 30} y="18" fontSize="10">Raw PDP</text><text x={colorbarX + 30} y="30" fontSize="10">power</text>
    </svg></div>
    <div className="muted-text">Color = run-wide raw PDP power in dB on the channel-daemon reference; no per-Sample normalization. Numerical floor is excluded only from color scaling. Absolute amplitude calibration: {samples[0]?.analytics?.channel.calibration?.amplitude || 'UNAVAILABLE'}. Magenta cross-dots = resolved components; hover for Path details.</div>
  </div>;
}

export default function Timeline({ experimentId, initialRunId = '', onBack }: { experimentId: string; initialRunId?: string; onBack?: () => void }) {
  const { update: updateContext } = useOperatorContext();
  const runs = useLoad<Run[]>(() => api.get(`/api/experiments/${encodeURIComponent(experimentId)}/runs`), [experimentId]);
  const [runId, setRunId] = useState(initialRunId);
  const [changeRun, setChangeRun] = useState(false);
  useEffect(() => { if (!runId && runs.data?.length) setRunId(runs.data[0].run_id); }, [runs.data, runId]);
  const result = useLoad<TimelineData>(() => runId ? api.get(`/api/experiments/${encodeURIComponent(experimentId)}/timeline?run_id=${encodeURIComponent(runId)}`) : Promise.resolve(null as any), [experimentId, runId]);
  const data = result.data;
  const run = runs.data?.find((row) => row.run_id === runId);
  const isRc = data?.environment === 'RC';
  const t0 = data?.t0_utc_ms ?? 0;
  const rel = (utc?: number | null) => utc == null ? 0 : (utc - t0) / 1000;
  const fullDomain: [number, number] = [rel(data?.master.start_utc_ms), Math.max(rel(data?.master.end_utc_ms), rel(data?.master.start_utc_ms) + 1)];
  const [viewport, setViewport] = useState<[number, number]>(fullDomain);
  const [selection, setSelection] = useState<[number, number]>(fullDomain);
  const [playhead, setPlayhead] = useState(fullDomain[0]);
  const [visible, setVisible] = useState({ phone: true, gnb: true, multipath: true, rc: true, events: true });
  const [view, setView] = useState<'overview' | 'timeline' | 'channel' | 'clips'>('overview');
  const [selectedEvent, setSelectedEvent] = useState<TimelineEvent | null>(null);
  useEffect(() => { setViewport(fullDomain); setSelection(fullDomain); setPlayhead(fullDomain[0]); setSelectedEvent(null); }, [data?.master.start_utc_ms, data?.master.end_utc_ms]);
  useEffect(() => { if (run && data) updateContext({ experimentId, environment: data.environment, configurationName: `${run.configuration_name || 'Snapshot unavailable'} v${run.configuration_version ?? '—'}`, runId, status: run.state, quality: run.quality_status || '' }); }, [run, runId, experimentId, data, updateContext]);

  const phone = addGaps((data?.samples || []).filter((row) => row.utc_epoch_ms != null).map((row) => ({ t: rel(row.utc_epoch_ms), power: row.battery_power_w == null ? null : row.battery_power_w * 1000, rsrp: row.ss_rsrp_dbm })), ['power', 'rsrp']);
  const gnb = addGaps((data?.gnb || []).map((row) => ({ t: rel(row.fetched_utc_ms), ul: row.ul_goodput_mbps, dl: row.dl_goodput_mbps, bler: row.ul_bler == null ? null : row.ul_bler * 100, harq: row.ul_harq_retransmission_ratio == null ? null : row.ul_harq_retransmission_ratio * 100 })), ['ul', 'dl', 'bler', 'harq']);
  const rcSamples = data?.rc_samples || [];
  const multipath = rcSamples.length
    ? rcSamples.map((row) => ({ t: rel((row.started_utc_ms + row.ended_utc_ms) / 2), rms: row.analytics?.channel.rms_delay_ns_filtered ?? null, k: row.analytics?.channel.k_factor_db_filtered ?? null, paths: row.analytics?.channel.effective_path_count ?? null }))
    : addGaps((data?.channel || []).map((row) => ({ t: rel(row.fetched_utc_ms), rms: row.rms_delay_ns, k: row.k_factor_db, paths: row.tap_count })), ['rms', 'k', 'paths']);
  const [sampleId, setSampleId] = useState<number | null>(null);
  useEffect(() => { if (rcSamples.length && !rcSamples.some((row) => row.id === sampleId)) setSampleId(rcSamples[0].id); }, [rcSamples, sampleId]);
  const selectedSample = rcSamples.find((row) => row.id === sampleId);
  const analytics = selectedSample?.analytics;
  const channel = analytics?.channel;
  const link = analytics?.link || {};
  const resolvedPaths = channel?.resolved_paths || channel?.effective_paths || [];
  const defaultDelayWindow = measuredDelayWindow(rcSamples);
  const selectedPdp = (selectedSample?.pdp || []).filter((point) => point.tau_ns >= defaultDelayWindow.start && point.tau_ns <= defaultDelayWindow.end).map((point) => ({ ...point, filtered_power_db: channel?.detection_threshold_db == null || point.power_db >= channel.detection_threshold_db ? point.power_db : null }));
  const [acChannel, setAcChannel] = useState<TimelineData | null>(null);
  const [loadingAcChannel, setLoadingAcChannel] = useState(false);
  useEffect(() => setAcChannel(null), [runId]);
  const loadAcChannel = async () => {
    if (acChannel || loadingAcChannel) return;
    setLoadingAcChannel(true);
    try { setAcChannel(await api.get(`/api/experiments/${encodeURIComponent(experimentId)}/timeline?run_id=${encodeURIComponent(runId)}&include_channel=true`)); }
    catch (cause) { toast('err', cause instanceof Error ? cause.message : String(cause)); }
    finally { setLoadingAcChannel(false); }
  };
  const rcSweep = rcSamples.map((row) => ({ angle: row.stirrer_angle_deg, sample: row.sample_index, paths: row.analytics?.channel.effective_path_count ?? null, rms: row.analytics?.channel.rms_delay_ns_filtered ?? null, k: row.analytics?.channel.k_factor_db_filtered ?? null, bler: typeof row.analytics?.link.ul_bler === 'number' ? Number(row.analytics.link.ul_bler) * 100 : null, harq: typeof row.analytics?.link.ul_harq_retx_rate === 'number' ? Number(row.analytics.link.ul_harq_retx_rate) * 100 : null, ul: row.analytics?.link.ul_goodput_mbps ?? null }));
  const selectSample = (sample: RcSample) => { setSampleId(sample.id); const range: [number, number] = [rel(sample.started_utc_ms), rel(sample.ended_utc_ms)]; setSelection(range); setViewport(range); setPlayhead(range[0]); };
  const availability = (first: number | null | undefined, last: number | null | undefined) => { if (first == null || last == null) return 'Unavailable'; const overlap = Math.max(0, Math.min(selection[1], rel(last)) - Math.max(selection[0], rel(first))); return overlap <= 0 ? 'Unavailable' : overlap >= selection[1] - selection[0] ? '100%' : 'Partial'; };
  const eventCategories = Array.from(new Map((data?.events || []).map((event) => [eventCategory(event), event])).entries());
  const drag = useRef<number | null>(null);
  const pointerTime = (event: React.PointerEvent<HTMLDivElement>) => { const box = event.currentTarget.getBoundingClientRect(); return viewport[0] + Math.max(0, Math.min(1, (event.clientX - box.left) / box.width)) * (viewport[1] - viewport[0]); };

  if (result.loading && !data) return <Spinner label="Loading Result Workspace…" />;
  if (result.error) return <ErrorBox error={result.error} />;
  if (!data || !run) return <EmptyState>No Run Result is available.</EmptyState>;

  return <div className="stack page-workspace result-workspace">
    <div className="page-head"><div><div className="title">Result Workspace</div><div className="subtitle">One Run · one Master Timeline · non-destructive Clip composition</div></div><div className="row"><button className="btn" onClick={() => setChangeRun(true)}>Change Run</button>{onBack && <button className="btn" onClick={onBack}>← Experiment History</button>}</div></div>
    <div className="tabs workspace-tabs" role="tablist" aria-label="Result views"><button className={view === 'overview' ? 'active' : ''} onClick={() => setView('overview')}>Overview</button><button className={view === 'timeline' ? 'active' : ''} onClick={() => setView('timeline')}>Timeline</button><button className={view === 'channel' ? 'active' : ''} onClick={() => setView('channel')}>{isRc ? 'Channel & RC' : 'Channel Data'}</button><button className={view === 'clips' ? 'active' : ''} onClick={() => setView('clips')}>Clips <Badge tone="muted">{data.clips?.length || 0}</Badge></button></div>
    <div className={`workspace-content stack result-view-${view}`}>
    {view === 'overview' && <>
    <div className="result-context"><div><span>Experiment</span><b>{experimentId}</b></div><div><span>Run</span><b>{runId}</b></div><div><span>{isRc ? 'Workflow Snapshot' : 'Configuration Snapshot'}</span><b>{isRc ? 'RC Workflow' : `${run.configuration_name || 'Unavailable'} v${run.configuration_version ?? '—'}`}</b></div><div><span>Environment</span><b>{data.environment}</b></div><div><span>Status</span><Badge tone={run.state === 'COMPLETE' || run.state === 'STOPPED' ? 'good' : 'muted'}>{run.state}</Badge></div><div><span>Quality</span><b>{run.quality_status || 'Not evaluated'}</b></div></div>
    {run.simulation ? <div className="simulation-banner">SIMULATED RUN · Permanently recorded in the Run Snapshot and export metadata</div> : null}

    <Card title="Data Completeness & Timeline Alignment" sub={`T+0 = ${data.t0_source || 'unavailable'} · no hidden time correction`}><div className="alignment-grid">{Object.entries(data.alignment || {}).map(([name, item]) => <div key={name}><b>{name.toUpperCase()}</b><Badge tone={item.status === 'ALIGNED' ? 'good' : 'warn'}>{item.status}</Badge><span>{item.count} records</span><span>Raw offset {item.raw_offset_ms == null ? '—' : `${item.raw_offset_ms} ms`}</span><span>Applied correction {item.applied_correction_ms} ms</span></div>)}</div></Card>
    </>}

    {view === 'channel' && <>
    {isRc && rcSamples.length > 0 && <><div className="rc-summary-grid"><Card title="Channel"><dl className="kpi-list"><div><dt>Resolved Paths</dt><dd title="Effective multipath components resolvable under the current bandwidth, noise threshold and detection settings.">{channel?.processing_status === 'OK' ? channel.resolved_path_count ?? channel.effective_path_count : 'Unavailable'}</dd></div><div><dt>RMS Delay</dt><dd>{number(channel?.rms_delay_ns_filtered, 1, ' ns')}</dd></div><div><dt>K-factor</dt><dd>{number(channel?.k_factor_db_filtered, 1, ' dB')}</dd></div><div><dt>Delay Resolution</dt><dd>{number(channel?.nominal_delay_resolution_ns, 2, ' ns')}</dd></div><div><dt>Noise / Threshold</dt><dd>{number(channel?.noise_floor_db, 1, ' dB')} / {number(channel?.detection_threshold_db, 1, ' dB')}</dd></div></dl>{channel?.processing_status !== 'OK' && <ErrorBox error={channel?.processing_error || 'Signal processing unavailable'} />}</Card><Card title="Link Reliability"><dl className="kpi-list"><div><dt>BLER</dt><dd>{percent(link.ul_bler)}</dd></div><div><dt>HARQ Retransmission</dt><dd>{percent(link.ul_harq_retx_rate)}</dd></div><div><dt>UL HARQ Tx / Retx</dt><dd>{link.ul_harq_tx ?? 'Unavailable'} / {link.ul_harq_retx ?? 'Unavailable'}</dd></div></dl></Card><Card title="Performance"><dl className="kpi-list"><div><dt>UL Goodput</dt><dd>{number(link.ul_goodput_mbps, 2, ' Mbps')}</dd></div><div><dt>DL Goodput</dt><dd>{number(link.dl_goodput_mbps, 2, ' Mbps')}</dd></div></dl></Card><Card title="Chamber"><dl className="kpi-list"><div><dt>Stirrer Angle</dt><dd>{number(selectedSample?.stirrer_angle_deg, 1, '°')}</dd></div><div><dt>Sample</dt><dd>{selectedSample?.sample_index ?? '—'} / {rcSamples.length}</dd></div><div><dt>Window</dt><dd>{selectedSample ? `${((selectedSample.ended_utc_ms - selectedSample.started_utc_ms) / 1000).toFixed(1)}s` : '—'}</dd></div></dl></Card></div>
      <Card title="RC Sample / Stirrer Track" sub="Click a Measurement Window to synchronize Timeline, PDP and every KPI."><div className="sample-regions">{rcSamples.map((sample) => <button key={sample.id} className={sample.id === sampleId ? 'active' : ''} onClick={() => selectSample(sample)}><b>Sample {sample.sample_index}</b><span>{sample.stirrer_angle_deg?.toFixed(1)}°</span><small>{new Date(sample.started_utc_ms).toISOString()} · MEASUREMENT</small></button>)}</div></Card>
      <Card title="PDP Heatmap" sub="Delay × RC Sample · one run-wide raw PDP dB scale"><PdpHeatmap samples={rcSamples} selectedId={sampleId} onSelect={selectSample} /></Card>
      <Card title="Channel → Link Window Association" sub="Every point uses Channel, BLER and HARQ from the same frozen Measurement Window; statistical association only."><div style={{ height: 260 }}><ResponsiveContainer><LineChart data={rcSweep}><XAxis dataKey="angle" label={{ value: 'Stirrer angle °', position: 'insideBottomRight' }} /><YAxis yAxisId="left" /><YAxis yAxisId="right" orientation="right" /><Tooltip /><Line yAxisId="left" dataKey="paths" name="Resolved paths" stroke="#8e44ad" /><Line yAxisId="right" dataKey="bler" name="BLER %" stroke="#c0392b" /><Line yAxisId="right" dataKey="harq" name="HARQ retx %" stroke="#e67e22" /></LineChart></ResponsiveContainer></div></Card>
    </>}
    </>}

    {view === 'timeline' && <>
    <Card className="timeline-control-card" title="Master Timeline Controls" sub="Drag to select a range; click a colored Event marker to inspect it.">
      <div className="timeline-toolbar">{Object.keys(visible).filter((key) => isRc || !['multipath', 'rc'].includes(key)).map((key) => <label key={key}><input type="checkbox" checked={visible[key as keyof typeof visible]} onChange={(event) => setVisible((old) => ({ ...old, [key]: event.target.checked }))} />{key}</label>)}<button className="btn sm" onClick={() => setViewport(fullDomain)}>Fit Run</button><button className="btn sm" onClick={() => setViewport(selection)}>Zoom to Selection</button>{visible.events && <div className="event-legend">{eventCategories.map(([category, event]) => <span key={category}><i style={{ background: eventColor(event) }} />{category.replace(/_/g, ' ')}</span>)}</div>}</div>
      <div className="master-ruler" onPointerDown={(event) => { const value = pointerTime(event); drag.current = value; setSelection([value, value]); }} onPointerMove={(event) => { if (drag.current != null) { const value = pointerTime(event); setSelection([Math.min(drag.current, value), Math.max(drag.current, value)]); } }} onPointerUp={(event) => { const value = pointerTime(event); if (drag.current != null) setSelection([Math.min(drag.current, value), Math.max(drag.current, value)]); drag.current = null; }}>
        <div className="selection-band" style={{ left: `${((selection[0] - viewport[0]) / (viewport[1] - viewport[0])) * 100}%`, width: `${((selection[1] - selection[0]) / (viewport[1] - viewport[0])) * 100}%` }} />
        <div className="playhead" style={{ left: `${((playhead - viewport[0]) / (viewport[1] - viewport[0])) * 100}%` }} />
        {visible.events && data.events.map((event, index) => { const time = rel(event.timestamp_utc_ms); const left = (time - viewport[0]) / (viewport[1] - viewport[0]) * 100; return left >= 0 && left <= 100 ? <button key={`${event.timestamp_utc_ms}-${index}`} className={`timeline-event-marker ${selectedEvent === event ? 'active' : ''}`} style={{ left: `clamp(7px, ${left}%, calc(100% - 7px))`, top: `${5 + index % 3 * 15}px`, color: eventColor(event) }} title={`${event.kind} · ${event.label}`} aria-label={`Event ${event.label} at ${relativeTime(time)}`} onPointerDown={(pointerEvent) => pointerEvent.stopPropagation()} onClick={(clickEvent) => { clickEvent.stopPropagation(); setSelectedEvent(event); setPlayhead(time); }} /> : null; })}
      </div>
      <div className="selection-inspector"><span>Start <b>T+{selection[0].toFixed(3)}s</b></span><span>End <b>T+{selection[1].toFixed(3)}s</b></span><span>Duration <b>{(selection[1] - selection[0]).toFixed(3)}s</b></span>{Object.entries(data.alignment).map(([name, item]) => <span key={name}>{name} <b>{availability(item.first_utc_ms, item.last_utc_ms)}</b></span>)}</div>
      {selectedEvent && <div className="event-inspector" style={{ borderColor: eventColor(selectedEvent) }}><i style={{ background: eventColor(selectedEvent) }} /><div><small>{selectedEvent.kind.replace(/_/g, ' ')}</small><b>{selectedEvent.label}</b></div><time>{relativeTime(rel(selectedEvent.timestamp_utc_ms))} · {new Date(selectedEvent.timestamp_utc_ms).toISOString()}</time><button className="icon-btn" aria-label="Close event details" onClick={() => setSelectedEvent(null)}>×</button></div>}
    </Card>
    {visible.phone && <Track title="Phone" sub="Battery power and RSRP · gaps are preserved" data={phone as any[]} domain={viewport} selection={selection} playhead={playhead} onPlayhead={setPlayhead} lines={[{ key: 'power', label: 'Power mW', color: '#3b5bdb' }, { key: 'rsrp', label: 'RSRP dBm', color: '#e67e22', axis: 'right' }]} />}
    {visible.gnb && <Track title="Link Reliability & Performance" sub="BLER is a fraction rendered as percent; HARQ is OAI interval-delta rate" data={gnb as any[]} domain={viewport} selection={selection} playhead={playhead} onPlayhead={setPlayhead} lines={[{ key: 'ul', label: 'UL Mbps', color: '#2e9e5b' }, { key: 'bler', label: 'BLER %', color: '#c0392b', axis: 'right' }, { key: 'harq', label: 'HARQ retx %', color: '#e67e22', axis: 'right' }]} />}
    {isRc && visible.multipath && <Track title="Resolved Multipath Evolution" sub="Resolved effective components and filtered metrics per frozen RC Measurement Window" data={multipath as any[]} domain={viewport} selection={selection} playhead={playhead} onPlayhead={setPlayhead} lines={[{ key: 'paths', label: 'Resolved paths', color: '#8e44ad' }, { key: 'rms', label: 'RMS delay ns', color: '#3b5bdb', axis: 'right' }, { key: 'k', label: 'K-factor dB', color: '#e67e22', axis: 'right' }]} />}
    </>}

    {view === 'channel' && <>
    {isRc ? <>
      <Card title="Selected PDP" sub={selectedSample ? `Sample ${selectedSample.sample_index} · ${number(selectedSample.stirrer_angle_deg, 1, '°')} · ${resolvedPaths.length} resolved component(s) · ${defaultDelayWindow.start.toFixed(0)}–${defaultDelayWindow.end.toFixed(0)} ns` : 'Select an RC Sample'}>{selectedPdp.length ? <div style={{ height: 300 }}><ResponsiveContainer><LineChart data={selectedPdp}><XAxis dataKey="tau_ns" type="number" unit=" ns" domain={[defaultDelayWindow.start, defaultDelayWindow.end]} /><YAxis unit=" dB" /><Tooltip /><Line dataKey="power_db" name="Raw PDP" dot={false} stroke="#8a8f98" strokeWidth={1} /><Line dataKey="filtered_power_db" name="Effective PDP" dot={false} stroke="#2e9e5b" strokeWidth={2} connectNulls={false} />{channel?.noise_floor_db != null && <ReferenceLine y={channel.noise_floor_db} stroke="#8a8f98" label="Noise floor" />}{channel?.detection_threshold_db != null && <ReferenceLine y={channel.detection_threshold_db} stroke="#c0392b" strokeDasharray="4 4" label="Detection threshold" />}{resolvedPaths.filter((path) => path.delay_ns >= defaultDelayWindow.start && path.delay_ns <= defaultDelayWindow.end).map((path) => <ReferenceDot key={path.path} x={path.delay_ns} y={path.power_db} r={6} fill={path.near_threshold ? '#ff8c00' : '#ff2d55'} stroke="#fff" strokeWidth={2} label={{ value: path.path_id || `P${path.path}`, position: 'top', fontSize: 10, fill: '#151922' }} />)}</LineChart></ResponsiveContainer></div> : <EmptyState>PDP unavailable.</EmptyState>}<div className="muted-text">Raw values are preserved. Filtering is a display mask · {defaultDelayWindow.source} · {channel?.processing_algorithm || 'legacy'} v{channel?.processing_version || 'unavailable'}.</div></Card>
      <Card title="Resolved Effective Multipath Components" sub="Detected components resolvable under the recorded bandwidth, threshold, prominence and minimum-separation settings.">{resolvedPaths.length ? <div className="table-wrap"><table className="data"><thead><tr><th>Path</th><th>Delay</th><th>Excess Delay</th><th>Absolute / Relative Power</th><th>Phase</th><th>Above Threshold</th><th>Prominence</th><th>Confidence</th></tr></thead><tbody>{resolvedPaths.map((path) => <tr key={path.path}><td><b>{path.path_id || `P${path.path}`}</b>{path.is_first_detected ? <><br /><small>FIRST</small></> : null}{path.is_strongest ? <><br /><small>STRONGEST</small></> : null}</td><td>{number(path.delay_ns, 2, ' ns')}</td><td>{number(path.excess_delay_ns, 2, ' ns')}</td><td>{number(path.power_db, 1, ' dB')} / {number(path.relative_power_db, 1, ' dB')}</td><td>{number(path.phase_deg, 1, '°')}<br /><small>{path.phase_calibration || 'UNCALIBRATED'}</small></td><td>{number(path.margin_above_threshold_db, 1, ' dB')}{path.near_threshold ? <><br /><Badge tone="warn">NEAR THRESHOLD</Badge></> : null}</td><td>{number(path.peak_prominence_db, 1, ' dB')}</td><td><Badge tone={path.confidence === 'HIGH' ? 'good' : path.confidence === 'LOW' ? 'warn' : 'accent'}>{path.confidence || 'UNKNOWN'}</Badge></td></tr>)}</tbody></table></div> : <EmptyState>No component passed the current resolution and detection constraints.</EmptyState>}</Card>
      <details className="details"><summary><b>Advanced Channel View</b><span className="muted-text">Complex response, phase, calibration and processing provenance</span></summary><div className="stack details-body">
        <div className="grid cols-3"><div className="kv"><dt>Effective bandwidth</dt><dd>{number(channel?.effective_bandwidth_mhz, 3, ' MHz')}</dd><dt>Nominal delay resolution</dt><dd>{number(channel?.nominal_delay_resolution_ns, 3, ' ns')}</dd><dt>Minimum separation</dt><dd>{number(channel?.minimum_resolvable_separation_ns, 3, ' ns')}</dd></div><div className="kv"><dt>Delay bins</dt><dd>{channel?.raw_delay_bin_count ?? 'Unavailable'}</dd><dt>Candidate / Effective peaks</dt><dd>{channel?.candidate_peak_count ?? '—'} / {channel?.effective_peak_count ?? '—'}</dd><dt>Resolved components</dt><dd>{channel?.resolved_path_count ?? channel?.effective_path_count ?? '—'}</dd></div><div className="kv"><dt>Frequency source</dt><dd>{channel?.frequency_response_source || 'Unavailable'}</dd><dt>Grid</dt><dd>{channel?.frequency_grid_consistency || 'Unavailable'}</dd><dt>Phase calibration</dt><dd>{channel?.calibration?.phase || 'UNCALIBRATED'}</dd></div></div>
        {(channel?.frequency_response || []).length ? <><Card title="Frequency Response Magnitude" sub="Same selected RC Measurement Window"><div style={{ height: 250 }}><ResponsiveContainer><LineChart data={channel?.frequency_response}><XAxis dataKey="frequency_mhz" type="number" domain={['dataMin', 'dataMax']} /><YAxis /><Tooltip /><Line dataKey="magnitude_db" name="|H(f)| dB" dot={false} stroke="#3b5bdb" /></LineChart></ResponsiveContainer></div></Card><Card title="Frequency Response Phase" sub={`${channel?.calibration?.phase || 'UNCALIBRATED'} · no physical phase conclusion is asserted`}><div style={{ height: 250 }}><ResponsiveContainer><LineChart data={channel?.frequency_response}><XAxis dataKey="frequency_mhz" type="number" domain={['dataMin', 'dataMax']} /><YAxis domain={[-180, 180]} /><Tooltip /><Line dataKey="phase_deg" name="Wrapped phase °" dot={false} stroke="#8e44ad" /></LineChart></ResponsiveContainer></div></Card></> : <EmptyState>Complex frequency response unavailable.</EmptyState>}
        <div className="grid cols-2"><Card title="Processing Metadata"><dl className="kpi-list"><div><dt>Algorithm</dt><dd>{channel?.processing_algorithm || 'Unavailable'} v{channel?.processing_version || '—'}</dd></div><div><dt>Peak detector</dt><dd>{channel?.peak_detection_method || 'Unavailable'}</dd></div><div><dt>Prominence threshold</dt><dd>{number(channel?.peak_prominence_threshold_db, 1, ' dB')}</dd></div><div><dt>Window</dt><dd>{channel?.window_function || 'Unavailable'}</dd></div><div><dt>Frequency spacing</dt><dd>{number(channel?.frequency_spacing_mhz, 6, ' MHz')}</dd></div><div><dt>Delay reference</dt><dd>{channel?.delay_reference || 'Unavailable'}</dd></div></dl></Card><Card title="Capability Boundaries"><p>Doppler analysis unavailable unless a sufficiently regular H(f,t) sequence is recorded.</p><p>AoA / AoD unavailable for single-antenna channel data.</p><p>Absolute phase is not interpreted unless phase calibration is explicitly recorded.</p></Card></div>
      </div></details>
    </> : <Card title="Advanced Channel Data" sub="AC Result stays focused on Phone, power, radio, link, events, Timeline and Clips. Raw channel data is loaded only on request." right={<button className="btn" disabled={loadingAcChannel} onClick={loadAcChannel}>{loadingAcChannel ? 'Loading…' : acChannel ? 'Loaded' : 'Load channel data'}</button>}>{acChannel ? (acChannel.cir?.pdp?.length ? <div><div className="muted-text">Raw AC channel data is available for research inspection; no default multipath interpretation is applied.</div><div style={{ height: 220 }}><ResponsiveContainer><LineChart data={acChannel.cir.pdp}><XAxis dataKey="tau_ns" type="number" /><YAxis /><Tooltip /><Line dataKey="power_db" name="Raw PDP" dot={false} stroke="#8a8f98" /></LineChart></ResponsiveContainer></div></div> : <EmptyState>No channel data was recorded for this AC Run.</EmptyState>) : <div className="muted-text">No CIR/PDP request has been made for this AC Result.</div>}</Card>}
    {isRc && <Card title="RC Sample Detail" sub="Audit table: Chamber → Channel → Link, all bound to one Measurement Window.">{rcSamples.length ? <div className="table-wrap"><table className="data"><thead><tr><th>Sample / Time</th><th>Angle</th><th>RSSP / Target</th><th>Resolved Paths</th><th>RMS Delay</th><th>K-factor</th><th>BLER</th><th>HARQ Retx / Errors</th><th>UL Goodput</th><th>Alignment / Status</th></tr></thead><tbody>{rcSamples.map((sample) => <tr key={sample.id} onClick={() => selectSample(sample)}><td>#{sample.sample_index}<br /><small>{new Date(sample.started_utc_ms).toISOString()}</small></td><td>{number(sample.stirrer_angle_deg, 1, '°')}</td><td>{number(sample.analytics?.radio.rssp_dbfs, 1, ' dBFS')} / {number(sample.analytics?.radio.target_rssp_dbfs, 1, ' dBFS')}</td><td>{sample.analytics?.channel.processing_status === 'OK' ? sample.analytics.channel.resolved_path_count ?? sample.analytics.channel.effective_path_count : 'Unavailable'}</td><td>{number(sample.analytics?.channel.rms_delay_ns_filtered, 1, ' ns')}</td><td>{number(sample.analytics?.channel.k_factor_db_filtered, 1, ' dB')}</td><td>{percent(sample.analytics?.link.ul_bler)}</td><td>{percent(sample.analytics?.link.ul_harq_retx_rate)} / {sample.analytics?.link.ul_harq_errors ?? 'Unavailable'}</td><td>{number(sample.analytics?.link.ul_goodput_mbps, 2, ' Mbps')}</td><td>{sample.analytics?.quality.alignment_status || 'MASTER_UTC_WINDOW'}<br /><Badge tone={sample.analytics?.quality.data_complete ? 'good' : 'warn'}>{sample.analytics?.quality.data_complete ? 'GOOD' : 'PARTIAL DATA'}</Badge></td></tr>)}</tbody></table></div> : <EmptyState>No RC Sample is available.</EmptyState>}</Card>}
    </>}

    {view === 'clips' && <ClipComposer experimentId={experimentId} runId={runId} domain={viewport} selection={selection} clips={data.clips || []} onSaved={result.reload} />}
    </div>

    {changeRun && <Modal title="Change Run" sub="The Result Workspace is locked to one Run. Changing clears the draft Clip." onClose={() => setChangeRun(false)} footer={<button className="btn" onClick={() => setChangeRun(false)}>Cancel</button>}><div className="stack">{(runs.data || []).map((item) => <button className="btn" key={item.run_id} onClick={() => { setRunId(item.run_id); setChangeRun(false); }}>{item.run_id} · {item.configuration_name || 'Legacy'} · {item.state}</button>)}</div></Modal>}
  </div>;
}

function ClipComposer({ experimentId, runId, domain, selection, clips, onSaved }: { experimentId: string; runId: string; domain: [number, number]; selection: [number, number]; clips: Clip[]; onSaved: () => void }) {
  const [name, setName] = useState('');
  const [segments, setSegments] = useState<Segment[]>([]);
  const [editing, setEditing] = useState<number | null>(null);
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState<Clip | null>(null);
  const next = useRef(1);
  useEffect(() => { setName(''); setSegments([]); setEditing(null); setDeleting(null); }, [runId]);
  const add = () => { if (selection[1] <= selection[0]) return; setSegments((old) => [...old, { key: next.current++, start: selection[0], end: selection[1], label: '' }]); };
  const move = (index: number, delta: number) => setSegments((old) => { const target = index + delta; if (target < 0 || target >= old.length) return old; const copy = [...old]; [copy[index], copy[target]] = [copy[target], copy[index]]; return copy; });
  const save = async () => { if (!name.trim() || !segments.length) return; setSaving(true); try { const body = { run_id: runId, name: name.trim(), segments: segments.map((segment) => ({ source_run_id: runId, source_start_relative_ms: segment.start * 1000, source_end_relative_ms: segment.end * 1000, label: segment.label })) }; if (editing) await api.put(`/api/clips/${editing}`, body); else await api.post(`/api/experiments/${encodeURIComponent(experimentId)}/clip`, body); toast('ok', `Clip saved with ${segments.length} Segment(s).`); setName(''); setSegments([]); setEditing(null); onSaved(); } catch (cause) { toast('err', cause instanceof Error ? cause.message : String(cause)); } finally { setSaving(false); } };
  const open = (clip: Clip, duplicate = false) => { setName(duplicate ? `${clip.label} Copy` : clip.label); setEditing(duplicate ? null : clip.id); setSegments(clip.segments.map((segment) => ({ key: next.current++, start: segment.source_start_relative_ms / 1000, end: segment.source_end_relative_ms / 1000, label: segment.label || '' }))); };
  const remove = async () => { if (!deleting) return; try { await api.delete(`/api/clips/${deleting.id}`); toast('ok', 'Clip deleted; raw Run data was not changed.'); setDeleting(null); onSaved(); } catch (cause) { toast('err', cause instanceof Error ? cause.message : String(cause)); } };
  const domainSpan = Math.max(0.001, domain[1] - domain[0]);
  const sourceLeft = (value: number) => Math.max(0, Math.min(100, (value - domain[0]) / domainSpan * 100));
  let cursor = 0;
  return <><Card title="Clip Composition" sub="Source Segments use the same viewport and horizontal scale as the Master Timeline." right={<button className="btn" onClick={add}>Add Selection to Clip</button>}><div className="stack"><Field label="Clip name"><input value={name} onChange={(event) => setName(event.target.value)} placeholder="baseline_stable" /></Field><div className="clip-source-timeline"><div className="clip-time-axis"><span>{relativeTime(domain[0])}</span><b>Source / Master time</b><span>{relativeTime(domain[1])}</span></div><div className="master-ruler clip-source-ruler"><div className="clip-selection-preview" style={{ left: `${sourceLeft(selection[0])}%`, width: `${Math.max(0, sourceLeft(selection[1]) - sourceLeft(selection[0]))}%` }} />{segments.map((segment, index) => { const start = Math.max(segment.start, domain[0]); const end = Math.min(segment.end, domain[1]); return end > start ? <div className="clip-source-segment" key={segment.key} style={{ left: `${sourceLeft(start)}%`, width: `${Math.max(.5, sourceLeft(end) - sourceLeft(start))}%`, top: `${7 + index % 2 * 23}px`, background: COLORS[index % COLORS.length] }} title={`Segment ${index + 1} · ${relativeTime(segment.start)} → ${relativeTime(segment.end)}`}>{index + 1}</div> : null; })}</div><div className="clip-timeline-note"><span><i className="selection-key" />Current selection</span><span>Colored blocks = source Segments · colors match the editor rows</span></div></div>{segments.length ? segments.map((segment, index) => { const clipStart = cursor; cursor += segment.end - segment.start; return <div className="clip-composition-row" key={segment.key} style={{ borderColor: COLORS[index % COLORS.length] }}><b>Segment {index + 1}</b><label>Start T+<input type="number" step="0.001" value={segment.start} onChange={(event) => setSegments((old) => old.map((item) => item.key === segment.key ? { ...item, start: Number(event.target.value) } : item))} /></label><label>End T+<input type="number" step="0.001" value={segment.end} onChange={(event) => setSegments((old) => old.map((item) => item.key === segment.key ? { ...item, end: Number(event.target.value) } : item))} /></label><span>Clip {clipStart.toFixed(3)} → {cursor.toFixed(3)}</span><input value={segment.label} placeholder="Label" onChange={(event) => setSegments((old) => old.map((item) => item.key === segment.key ? { ...item, label: event.target.value } : item))} /><button className="btn sm" disabled={index === 0} onClick={() => move(index, -1)}>↑</button><button className="btn sm" disabled={index === segments.length - 1} onClick={() => move(index, 1)}>↓</button><button className="btn sm" onClick={() => setSegments((old) => [...old.slice(0, index + 1), { ...segment, key: next.current++ }, ...old.slice(index + 1)])}>Duplicate</button><button className="btn sm danger" onClick={() => setSegments((old) => old.filter((item) => item.key !== segment.key))}>Delete</button></div>; }) : <EmptyState>Select a Master Timeline range, then add it to the draft.</EmptyState>}<button className="btn primary" disabled={saving || !name.trim() || !segments.length || segments.some((segment) => segment.end <= segment.start)} onClick={save}>{saving ? 'Saving…' : editing ? 'Update Clip' : 'Save Clip'}</button><div className="table-wrap"><table className="data"><thead><tr><th>Clip</th><th>Segments</th><th>Duration</th><th>Created</th><th>Actions</th></tr></thead><tbody>{clips.map((clip) => <tr key={clip.id}><td>{clip.label}</td><td>{clip.segments.length}</td><td>{(clip.segments.reduce((sum, segment) => sum + segment.source_end_relative_ms - segment.source_start_relative_ms, 0) / 1000).toFixed(3)}s</td><td>{clip.created_utc?.replace('T', ' ').slice(0, 19)}</td><td><button className="btn sm" onClick={() => open(clip)}>Edit</button><button className="btn sm" onClick={() => open(clip, true)}>Duplicate</button><button className="btn sm" onClick={() => window.open(`/api/clips/${clip.id}/download`, '_blank')}>Export</button><button className="btn sm danger" onClick={() => setDeleting(clip)}>Delete</button></td></tr>)}</tbody></table></div></div></Card>{deleting && <Modal title="Delete Clip?" sub="Only the derivative Clip and Segment list are deleted. Raw Run data remains immutable." onClose={() => setDeleting(null)} footer={<><button className="btn" onClick={() => setDeleting(null)}>Cancel</button><button className="btn danger" onClick={remove}>Delete Clip</button></>}><p><b>{deleting.label}</b> · {deleting.segments.length} Segment(s)</p></Modal>}</>;
}
