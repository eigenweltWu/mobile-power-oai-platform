import { useState } from 'react';
import { api } from '../api';
import { Card, EmptyState, ErrorBox, Spinner, Field, Badge, toast } from '../components/ui';
import { useLoad } from '../components/DataView';
import { LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine } from 'recharts';

type Ack = { id: number; seq: number; direction: string; pc_send_ms: number | null; pc_recv_ms: number | null; rtt_ms: number | null };
type Sample = { id: number; elapsed_realtime_ns: number; utc_epoch_ms: number | null; battery_power_w: number | null; ss_rsrp_dbm: number | null; run_id: string };
type GnbSnapshot = { fetched_utc_ms: number; ul_goodput_mbps: number | null; dl_goodput_mbps: number | null };

export default function Timeline({ experimentId }: { experimentId: string }) {
  const { data, error, loading } = useLoad<any>(() => api.get(`/api/experiments/${encodeURIComponent(experimentId)}/timeline`), [experimentId]);
  const [clip, setClip] = useState({ start_ms: '', end_ms: '', label: '' });

  const samples: Sample[] = data?.samples ?? [];
  const acks: Ack[] = data?.acks ?? [];
  const gnb: GnbSnapshot[] = data?.gnb ?? [];

  // build chart rows: x = elapsed seconds relative to first sample
  const t0 = samples.length ? samples[0].elapsed_realtime_ns : 0;
  const rows = samples.map((s) => ({
    t: (s.elapsed_realtime_ns - t0) / 1e9,
    power: s.battery_power_w != null ? s.battery_power_w * 1000 : null, // mW
    rsrp: s.ss_rsrp_dbm,
  }));

  // gNB goodput rows aligned on the same time base via UTC: the phone records
  // utc_epoch_ms per sample, the SnapshotCollector stamps fetched_utc_ms.
  const t0Utc = samples.find((s) => s.utc_epoch_ms != null)?.utc_epoch_ms ?? null;
  const gnbRows = t0Utc != null
    ? gnb
        .filter((g) => g.fetched_utc_ms != null)
        .map((g) => ({
          t: (g.fetched_utc_ms - t0Utc) / 1000,
          ulg: g.ul_goodput_mbps,
          dlg: g.dl_goodput_mbps,
        }))
    : [];

  const ackMarkers = acks
    .filter((a) => a.pc_send_ms != null)
    .map((a) => ({ x: ((a.pc_send_ms! * 1e6 - t0) / 1e9), direction: a.direction, rtt: a.rtt_ms }));

  const saveClip = async () => {
    const start = parseFloat(clip.start_ms);
    const end = parseFloat(clip.end_ms);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
      toast('err', '剪辑区间无效');
      return;
    }
    try {
      const runId = samples.length ? samples[0].run_id : undefined;
      const r = await api.post<any>(`/api/experiments/${encodeURIComponent(experimentId)}/clip`, {
        run_id: runId, start_ms: start, end_ms: end, label: clip.label,
      });
      toast('ok', `已保存剪辑: ${r.path} (${r.n_rows} 行)`);
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    }
  };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">历史结果 · {experimentId}</div>
          <div className="subtitle">phone power / RSRP / gNB goodput timeline with ACK markers</div>
        </div>
      </div>

      <Card title="Timeline">
        {loading ? <Spinner /> : error ? <ErrorBox error={error} /> : samples.length === 0 ? (
          <EmptyState>该实验暂无手机数据。</EmptyState>
        ) : (
          <>
            <div style={{ height: 320 }}>
              <ResponsiveContainer>
                <LineChart data={rows}>
                  <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '秒', position: 'insideBottomRight', fontSize: 10 }} />
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
                总时长 {rows.length ? rows[rows.length - 1].t.toFixed(1) : 0} 秒 · {acks.length} 个关键时间戳
              </span>
            </div>
          </>
        )}
      </Card>

      <Card
        title="gNB Goodput · UL / DL"
        sub={gnbRows.length
          ? `UL ${gnbRows.filter((g) => g.ulg != null).length} 点 · DL ${gnbRows.filter((g) => g.dlg != null).length} 点 · 与手机采样按 UTC 对齐`
          : '本实验未记录 gNB goodput（SnapshotCollector 未运行或 OAI 不可达）'}
      >
        {gnbRows.length === 0 ? (
          <EmptyState>暂无 gNB goodput 数据。</EmptyState>
        ) : (
          <>
            <div style={{ height: 260 }}>
              <ResponsiveContainer>
                <LineChart data={gnbRows}>
                  <XAxis dataKey="t" type="number" domain={['dataMin', 'dataMax']} tick={{ fontSize: 10 }} label={{ value: '秒', position: 'insideBottomRight', fontSize: 10 }} />
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
              <span className="mono" style={{ fontSize: 12, color: 'var(--muted)' }}>
                {gnbRows.length} 个 gNB 快照
              </span>
            </div>
          </>
        )}
      </Card>

      <Card title="数据剪辑（左右拖动选择区间并保存）">
        <div className="grid cols-3">
          <Field label="起始 (ms)"><input className="mono" value={clip.start_ms} onChange={(e) => setClip({ ...clip, start_ms: e.target.value })} placeholder={String(t0)} /></Field>
          <Field label="结束 (ms)"><input className="mono" value={clip.end_ms} onChange={(e) => setClip({ ...clip, end_ms: e.target.value })} /></Field>
          <Field label="标签"><input value={clip.label} onChange={(e) => setClip({ ...clip, label: e.target.value })} placeholder="clip 名称" /></Field>
        </div>
        <div style={{ marginTop: 12 }}>
          <button className="btn primary" onClick={saveClip}>保存剪辑</button>
        </div>
        <div className="notice-box" style={{ marginTop: 12 }}>
          剪辑区间基于手机 elapsed_realtime_ns（毫秒）。可在上方图表中左右拖动查看完整时间轴。
        </div>
      </Card>
    </div>
  );
}
