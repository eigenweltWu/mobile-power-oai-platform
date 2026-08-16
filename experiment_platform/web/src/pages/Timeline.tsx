import { useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { Card, EmptyState, ErrorBox, Spinner, Field, Badge, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';

type Ack = { id: number; seq: number; direction: string; pc_send_ms: number | null; pc_recv_ms: number | null; rtt_ms: number | null };
type Sample = { id: number; elapsed_realtime_ns: number; utc_epoch_ms: number | null; battery_power_w: number | null; ss_rsrp_dbm: number | null; run_id: string };
type GnbSnapshot = { fetched_utc_ms: number; ul_goodput_mbps: number | null; dl_goodput_mbps: number | null };
type ChannelMetric = { run_id: string; fetched_utc_ms: number; rms_delay_ns: number | null; k_factor_db: number | null; tap_count: number | null; peak_db: number | null; noise_db: number | null };
type Cir = { dt_ns: number; n_samples: number; pdp: { tau_ns: number; power_db: number }[] };
type Run = { run_id: string; state: string | null; started_utc_ms: number | null; ended_utc_ms: number | null };
type ClipRow = { id: number; run_id: string | null; start_ms: number | null; end_ms: number | null; label: string | null; created_utc: string | null };

type Segment = { id: number; start: number; end: number; label: string };

const SEG_COLORS = ['#3b5bdb', '#2e9e5b', '#e67e22', '#8e44ad', '#c0392b'];

export default function Timeline({ experimentId }: { experimentId: string }) {
  const { data, error, loading, reload } = useLoad<any>(() => api.get(`/api/experiments/${encodeURIComponent(experimentId)}/timeline`), [experimentId]);

  const samples: Sample[] = data?.samples ?? [];
  const acks: Ack[] = data?.acks ?? [];
  const gnb: GnbSnapshot[] = data?.gnb ?? [];
  const runs: Run[] = data?.runs ?? [];
  const savedClips: ClipRow[] = data?.clips ?? [];

  // ---- fused time axis: t=0 is the FIRST pre-run clock sync (task 5) ----
  const t0: number | null = data?.t0_utc_ms ?? samples.find((s) => s.utc_epoch_ms != null)?.utc_epoch_ms ?? null;
  const rel = (utc: number | null | undefined) => (utc == null || t0 == null ? null : (utc - t0) / 1000);

  const rows = samples
    .filter((s) => s.utc_epoch_ms != null)
    .map((s) => ({
      t: rel(s.utc_epoch_ms)!,
      power: s.battery_power_w != null ? s.battery_power_w * 1000 : null, // mW
      rsrp: s.ss_rsrp_dbm,
    }));

  const gnbRows = gnb
    .filter((g) => g.fetched_utc_ms != null)
    .map((g) => ({ t: rel(g.fetched_utc_ms)!, ulg: g.ul_goodput_mbps, dlg: g.dl_goodput_mbps }));

  const channel: ChannelMetric[] = data?.channel ?? [];
  const cir: Cir | null = data?.cir ?? null;
  const channelRows = channel
    .filter((c) => c.fetched_utc_ms != null)
    .map((c) => ({
      t: rel(c.fetched_utc_ms)!,
      rms_ns: c.rms_delay_ns,
      k_db: c.k_factor_db,
      taps: c.tap_count,
      peak: c.peak_db,
      noise: c.noise_db,
    }));

  // ---- key timestamp markers on the fused axis ----
  type Marker = { t: number; kind: string; label: string };
  const markers: Marker[] = [];
  for (const r of runs) {
    const a = rel(r.started_utc_ms); if (a != null) markers.push({ t: a, kind: 'run_start', label: `${r.run_id} start` });
    const b = rel(r.ended_utc_ms); if (b != null) markers.push({ t: b, kind: 'run_end', label: `${r.run_id} end` });
  }
  const phoneFirst = samples.find((s) => s.utc_epoch_ms != null);
  const phoneLast = [...samples].reverse().find((s) => s.utc_epoch_ms != null);
  if (phoneFirst) markers.push({ t: rel(phoneFirst.utc_epoch_ms)!, kind: 'phone_span', label: 'phone 首样本' });
  if (phoneLast && phoneLast !== phoneFirst) markers.push({ t: rel(phoneLast.utc_epoch_ms)!, kind: 'phone_span', label: 'phone 末样本' });
  if (gnb.length) {
    markers.push({ t: rel(gnb[0].fetched_utc_ms)!, kind: 'gnb_span', label: 'gNB 首快照' });
    const lastG = gnb[gnb.length - 1];
    markers.push({ t: rel(lastG.fetched_utc_ms)!, kind: 'gnb_span', label: 'gNB 末快照' });
  }
  for (const a of acks) {
    if (a.pc_send_ms == null) continue;
    markers.push({ t: rel(a.pc_send_ms)!, kind: a.direction === 'pc_stop' ? 'pc_stop' : 'ack', label: `ACK seq${a.seq}` });
  }
  markers.sort((a, b) => a.t - b.t);

  const ackMarkers = markers
    .filter((m) => m.kind === 'ack' || m.kind === 'pc_stop')
    .map((m) => ({ x: m.t, direction: m.kind === 'pc_stop' ? 'pc_stop' : 'ack' }));

  const duration = markers.length ? markers[markers.length - 1].t : (rows.length ? rows[rows.length - 1].t : 0);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">历史结果 · {experimentId}</div>
          <div className="subtitle">
            fused timeline — t=0 = 首次对时{data?.t0_source ? `（${data.t0_source}）` : ''}，单位秒，手机/gNB 关键时间戳已标注
          </div>
        </div>
      </div>

      <Card title="Timeline">
        {loading ? <Spinner /> : error ? <ErrorBox error={error} /> : rows.length === 0 ? (
          <EmptyState>该实验暂无手机数据（可先在 Phone Sync 页提取）。</EmptyState>
        ) : (
          <>
            <div style={{ height: 320 }}>
              <ResponsiveContainer>
                <LineChart data={rows}>
                  <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '秒 (对时=0)', position: 'insideBottomRight', fontSize: 10 }} />
                  <YAxis yAxisId="p" tick={{ fontSize: 10 }} label={{ value: '功率 mW', angle: -90, position: 'insideLeft', fontSize: 10 }} />
                  <YAxis yAxisId="r" orientation="right" tick={{ fontSize: 10 }} label={{ value: 'RSRP dBm', angle: 90, position: 'insideRight', fontSize: 10 }} />
                  <Tooltip />
                  {ackMarkers.map((m, i) => (
                    <ReferenceLine key={i} x={m.x} yAxisId="p" stroke={m.direction === 'pc_stop' ? '#d93025' : '#1a9d54'} strokeDasharray="4 4" />
                  ))}
                  <Line yAxisId="p" dataKey="power" name="功率 mW" dot={false} isAnimationActive={false} stroke="#3b5bdb" />
                  <Line yAxisId="r" dataKey="rsrp" name="RSRP" dot={false} isAnimationActive={false} stroke="#d96a1f" />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="row gap-sm" style={{ marginTop: 10 }}>
              <Badge tone="good">绿虚线 = ACK/下行时间戳</Badge>
              <Badge tone="bad">红虚线 = PC 停止时间戳</Badge>
              <span className="mono" style={{ fontSize: 12, color: 'var(--muted)' }}>
                总时长 {duration.toFixed(1)} 秒 · {markers.length} 个关键时间戳
              </span>
            </div>
          </>
        )}
      </Card>

      <ClipWorkbench
        experimentId={experimentId}
        duration={Math.max(duration, 1)}
        markers={markers}
        savedClips={savedClips}
        onSaved={reload}
      />

      <Card
        title="gNB Goodput · UL / DL"
        sub={gnbRows.length
          ? `UL ${gnbRows.filter((g) => g.ulg != null).length} 点 · DL ${gnbRows.filter((g) => g.dlg != null).length} 点 · 与手机采样按对时轴对齐`
          : '本实验未记录 gNB goodput（SnapshotCollector 未运行或 OAI 不可达）'}
      >
        {gnbRows.length === 0 ? (
          <EmptyState>暂无 gNB goodput 数据。</EmptyState>
        ) : (
          <>
            <div style={{ height: 260 }}>
              <ResponsiveContainer>
                <LineChart data={gnbRows}>
                  <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '秒 (对时=0)', position: 'insideBottomRight', fontSize: 10 }} />
                  <YAxis tick={{ fontSize: 10 }} label={{ value: 'Mbps', angle: -90, position: 'insideLeft', fontSize: 10 }} />
                  <Tooltip />
                  {ackMarkers.map((m, i) => (
                    <ReferenceLine key={i} x={m.x} stroke={m.direction === 'pc_stop' ? '#d93025' : '#1a9d54'} strokeDasharray="4 4" />
                  ))}
                  <Line dataKey="ulg" name="UL goodput Mbps" dot={false} isAnimationActive={false} stroke="#2e9e5b" strokeWidth={2} connectNulls />
                  <Line dataKey="dlg" name="DL goodput Mbps" dot={false} isAnimationActive={false} stroke="#3b5bdb" strokeWidth={2} connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="row gap-sm" style={{ marginTop: 10 }}>
              <span className="row" style={{ gap: 6, fontSize: 12 }}><span style={{ width: 10, height: 3, background: '#2e9e5b' }} />UL goodput</span>
              <span className="row" style={{ gap: 6, fontSize: 12 }}><span style={{ width: 10, height: 3, background: '#3b5bdb' }} />DL goodput</span>
              <span className="mono" style={{ fontSize: 12, color: 'var(--muted)' }}>{gnbRows.length} 个 gNB 快照</span>
            </div>
          </>
        )}
      </Card>

      <Card
        title="CIR · 多径指标"
        sub={channelRows.length
          ? `RMS delay spread / K-factor / tap 数 · ${channelRows.length} 帧 · 与手机采样按对时轴对齐`
          : '本实验未记录复信道（ChannelCollector 未运行或 OAI daemon 不可达）'}
      >
        {channelRows.length === 0 ? (
          <EmptyState>暂无 CIR 多径指标数据。</EmptyState>
        ) : (
          <>
            <div style={{ height: 240 }}>
              <ResponsiveContainer>
                <LineChart data={channelRows}>
                  <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '秒 (对时=0)', position: 'insideBottomRight', fontSize: 10 }} />
                  <YAxis yAxisId="d" tick={{ fontSize: 10 }} label={{ value: 'delay ns', angle: -90, position: 'insideLeft', fontSize: 10 }} />
                  <YAxis yAxisId="k" orientation="right" tick={{ fontSize: 10 }} label={{ value: 'K dB', angle: 90, position: 'insideRight', fontSize: 10 }} />
                  <Tooltip />
                  <Line yAxisId="d" dataKey="rms_ns" name="RMS delay (ns)" dot={false} isAnimationActive={false} stroke="#8e44ad" strokeWidth={2} connectNulls />
                  <Line yAxisId="k" dataKey="k_db" name="K-factor (dB)" dot={false} isAnimationActive={false} stroke="#e67e22" connectNulls />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div className="row gap-sm" style={{ marginTop: 10 }}>
              <span className="row" style={{ gap: 6, fontSize: 12 }}><span style={{ width: 10, height: 3, background: '#8e44ad' }} />RMS delay spread</span>
              <span className="row" style={{ gap: 6, fontSize: 12 }}><span style={{ width: 10, height: 3, background: '#e67e22' }} />K-factor</span>
            </div>
          </>
        )}
      </Card>

      <Card
        title="CIR · 功率延迟谱 (PDP)"
        sub={cir ? `|h(τ)|² vs 延迟 · ${cir.n_samples} 复采样 @ ${cir.dt_ns.toFixed(2)} ns/点（已降采样）` : '暂无复信道数据'}
      >
        {!cir || !cir.pdp.length ? (
          <EmptyState>暂无 CIR 功率延迟谱。</EmptyState>
        ) : (
          <div style={{ height: 240 }}>
            <ResponsiveContainer>
              <LineChart data={cir.pdp}>
                <XAxis dataKey="tau_ns" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '延迟 ns', position: 'insideBottomRight', fontSize: 10 }} />
                <YAxis tick={{ fontSize: 10 }} label={{ value: '|h|² dB', angle: -90, position: 'insideLeft', fontSize: 10 }} />
                <Tooltip labelFormatter={(v) => `延迟 ${v} ns`} />
                <Line dataKey="power_db" name="|h(τ)|² (dB)" dot={false} isAnimationActive={false} stroke="#2e9e5b" strokeWidth={1.5} />
              </LineChart>
            </ResponsiveContainer>
          </div>
        )}
      </Card>
    </div>
  );
}

/* ------------------------------------------------------------------------- */
/* Video-style clip workbench: draggable segments on the fused time axis      */
/* ------------------------------------------------------------------------- */

function ClipWorkbench({ experimentId, duration, markers, savedClips, onSaved }: {
  experimentId: string;
  duration: number;
  markers: { t: number; kind: string; label: string }[];
  savedClips: ClipRow[];
  onSaved: () => void;
}) {
  const [segments, setSegments] = useState<Segment[]>([{ id: 1, start: 0, end: Math.min(30, duration), label: '' }]);
  const [saving, setSaving] = useState<number | null>(null);
  const trackRef = useRef<HTMLDivElement>(null);
  const drag = useRef<{ segId: number; edge: 'start' | 'end' | 'move'; x0: number; s0: number; e0: number } | null>(null);
  const segIdRef = useRef(2);

  const secPerPx = () => {
    const w = trackRef.current?.clientWidth ?? 1;
    return duration / Math.max(w - 20, 1); // minus handle widths
  };

  useEffect(() => {
    const onMove = (e: PointerEvent) => {
      const d = drag.current;
      if (!d) return;
      const ds = (e.clientX - d.x0) * secPerPx();
      setSegments((prev) => prev.map((s) => {
        if (s.id !== d.segId) return s;
        let start = s.start;
        let end = s.end;
        if (d.edge === 'start') start = Math.max(0, Math.min(d.s0 + ds, end - 0.5));
        else if (d.edge === 'end') end = Math.min(duration, Math.max(d.e0 + ds, start + 0.5));
        else { // move whole segment
          const w = d.e0 - d.s0;
          start = Math.max(0, Math.min(duration - w, d.s0 + ds));
          end = start + w;
        }
        return { ...s, start, end };
      }));
    };
    const onUp = () => { drag.current = null; };
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', onUp);
    return () => {
      window.removeEventListener('pointermove', onMove);
      window.removeEventListener('pointerup', onUp);
    };
  }, [duration]);

  const beginDrag = (e: React.PointerEvent, seg: Segment, edge: 'start' | 'end' | 'move') => {
    e.preventDefault();
    e.stopPropagation();
    drag.current = { segId: seg.id, edge, x0: e.clientX, s0: seg.start, e0: seg.end };
  };

  const addSegment = () => {
    const id = segIdRef.current++;
    setSegments((prev) => [...prev, { id, start: 0, end: Math.min(30, duration), label: '' }]);
  };

  const updateSegment = (id: number, patch: Partial<Segment>) => {
    setSegments((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  };

  const removeSegment = (id: number) => {
    setSegments((prev) => prev.filter((s) => s.id !== id));
  };

  const saveSegment = async (seg: Segment) => {
    if (seg.end <= seg.start) { toast('err', '区间无效：结束需晚于开始'); return; }
    setSaving(seg.id);
    try {
      const r = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/clip`, {
        run_id: null, start_ms: seg.start * 1000, end_ms: seg.end * 1000, label: seg.label,
      });
      toast('ok', `已另存副本 #${r.clip_id}：${r.n_rows} 行 (${seg.start.toFixed(1)}s–${seg.end.toFixed(1)}s)`);
      onSaved();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  };

  const pct = (t: number) => `${(t / duration) * 100}%`;
  const ticks: number[] = [];
  const tickStep = duration > 600 ? 60 : duration > 120 ? 10 : 5;
  for (let t = 0; t <= duration; t += tickStep) ticks.push(t);

  const MARK_COLORS: Record<string, string> = {
    run_start: '#3b5bdb', run_end: '#1f3a93', ack: '#1a9d54', pc_stop: '#d93025',
    phone_span: '#8a8f98', gnb_span: '#8e44ad',
  };

  return (
    <Card
      title="剪辑工作台"
      sub="t=0 为首次对时；拖动色块手柄调整起止，可添加多个片段，每段另存为融合副本（手机+gNB+信道）"
      right={<button className="btn sm" onClick={addSegment}>+ 添加片段</button>}
    >
      <div className="clip-track-wrap">
        <div className="clip-ticks">
          {ticks.map((t) => (
            <span key={t} className="clip-tick" style={{ left: pct(t) }}>{t}s</span>
          ))}
        </div>
        <div className="clip-track" ref={trackRef}>
          {markers.map((m, i) => (
            <div key={i} className="clip-marker" style={{ left: pct(m.t), background: MARK_COLORS[m.kind] ?? '#888' }}
              title={`${m.label} @ ${m.t.toFixed(1)}s`} />
          ))}
          {segments.map((seg, i) => (
            <div key={seg.id} className="clip-seg" style={{ left: pct(seg.start), width: `${((seg.end - seg.start) / duration) * 100}%`, background: `${SEG_COLORS[i % SEG_COLORS.length]}33`, borderColor: SEG_COLORS[i % SEG_COLORS.length] }}
              onPointerDown={(e) => beginDrag(e, seg, 'move')}>
              <div className="clip-handle left" onPointerDown={(e) => beginDrag(e, seg, 'start')} />
              <span className="clip-seg-label mono">
                {seg.start.toFixed(1)}–{seg.end.toFixed(1)}s
              </span>
              <div className="clip-handle right" onPointerDown={(e) => beginDrag(e, seg, 'end')} />
            </div>
          ))}
        </div>
        <div className="row gap-sm" style={{ marginTop: 6 }}>
          <Badge tone="accent">蓝 = run start/end</Badge>
          <Badge tone="good">绿 = ACK</Badge>
          <Badge tone="bad">红 = PC stop</Badge>
          <Badge tone="muted">灰 = phone 首末样本</Badge>
          <Badge tone="muted">紫 = gNB 首末快照</Badge>
        </div>
      </div>

      {segments.map((seg, i) => (
        <div key={seg.id} className="row clip-seg-controls" style={{ borderColor: SEG_COLORS[i % SEG_COLORS.length] }}>
          <span className="clip-dot" style={{ background: SEG_COLORS[i % SEG_COLORS.length] }} />
          <Field label="开始 (秒)">
            <input className="mono" type="number" step="0.1" min={0} max={duration} value={seg.start.toFixed(1)}
              onChange={(e) => updateSegment(seg.id, { start: Math.max(0, Math.min(duration - 0.5, parseFloat(e.target.value) || 0)) })} />
          </Field>
          <Field label="结束 (秒)">
            <input className="mono" type="number" step="0.1" min={0} max={duration} value={seg.end.toFixed(1)}
              onChange={(e) => updateSegment(seg.id, { end: Math.min(duration, Math.max(0.5, parseFloat(e.target.value) || 0)) })} />
          </Field>
          <Field label="标签">
            <input value={seg.label} onChange={(e) => updateSegment(seg.id, { label: e.target.value })} placeholder="clip 名称" />
          </Field>
          <div className="row" style={{ alignSelf: 'flex-end' }}>
            <button className="btn sm primary" disabled={saving === seg.id} onClick={() => saveSegment(seg)}>
              {saving === seg.id ? '保存中…' : '另存为副本'}
            </button>
            {segments.length > 1 && (
              <button className="btn sm danger" onClick={() => removeSegment(seg.id)}>删除</button>
            )}
          </div>
        </div>
      ))}

      <div className="table-wrap" style={{ marginTop: 12 }}>
        <table className="data">
          <thead>
            <tr>
              <th>#</th><th>标签</th><th>区间 (秒, 对时=0)</th><th>时长</th><th>创建时间</th><th>run</th><th></th>
            </tr>
          </thead>
          <tbody>
            {savedClips.length === 0 ? (
              <tr><td colSpan={7} style={{ color: 'var(--muted)' }}>暂无已保存副本</td></tr>
            ) : savedClips.map((c) => (
              <tr key={c.id}>
                <td className="mono">{c.id}</td>
                <td>{c.label || '—'}</td>
                <td className="mono">{((c.start_ms ?? 0) / 1000).toFixed(1)} – {((c.end_ms ?? 0) / 1000).toFixed(1)}</td>
                <td className="mono">{(((c.end_ms ?? 0) - (c.start_ms ?? 0)) / 1000).toFixed(1)} s</td>
                <td className="mono">{c.created_utc ? c.created_utc.replace('T', ' ').slice(0, 19) : '—'}</td>
                <td className="mono">{c.run_id ?? '—'}</td>
                <td>
                  <button className="btn sm" onClick={() => window.open(`/api/clips/${c.id}/download`, '_blank')}>
                    下载 CSV
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Card>
  );
}
