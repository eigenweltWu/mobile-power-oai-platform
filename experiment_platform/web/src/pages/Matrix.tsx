import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Condition, Experiment, Run } from '../types';
import { computeMetrics, loadMergedMap, type RunMetrics } from '../metrics';
import { Badge, Card, EmptyState, ErrorBox, Spinner, StatCard } from '../components/ui';
import { fmt, num } from '../format';

interface Row extends RunMetrics {
  run_id: string;
  state: string;
  quality_status: string | null;
  device_id: string | null;
  environment: string | null;
  target_rsrp_dbm: number | null;
  bandwidth_mhz: number | null;
  pusch_target_snr_x10: number | null;
  scheduler_mode: string | null;
  n_prb_cond: number | null;
  condition_id: string;
}

function rowClass(r: Row): string {
  const q = r.quality_status ?? r.state;
  if (q === 'COMPLETE' || q === 'PASS') return 'complete';
  if (q === 'WARNING') return 'warning';
  if (q === 'FAILED') return 'failed';
  return '';
}

export default function Matrix({ nav }: { nav: (p: string) => void }) {
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [e, c, r] = await Promise.all([
          api.get<Experiment[]>('/api/experiments'),
          api.get<Condition[]>('/api/conditions'),
          api.get<Run[]>('/api/runs'),
        ]);
        if (cancelled) return;
        const envMap = new Map(e.map((x) => [x.experiment_id, x.environment]));
        const condMap = new Map(c.map((x) => [x.condition_id, x]));
        const merged = await loadMergedMap(r.map((x) => x.run_id), 4, (done, total) => {
          if (!cancelled) setProgress({ done, total });
        });
        if (cancelled) return;
        setRows(
          r.map((run) => {
            const cond = condMap.get(run.condition_id);
            const m = computeMetrics(merged.get(run.run_id) ?? []);
            return {
              ...m,
              run_id: run.run_id,
              condition_id: run.condition_id,
              state: run.state,
              quality_status: run.quality_status ?? null,
              device_id: run.device_id ?? null,
              environment: cond?.environment ?? envMap.get(run.experiment_id) ?? null,
              target_rsrp_dbm: num(cond?.target_rsrp_dbm),
              bandwidth_mhz: num(cond?.bandwidth_mhz),
              pusch_target_snr_x10: num(cond?.pusch_target_snr_x10),
              scheduler_mode: cond?.scheduler_mode ?? null,
              n_prb_cond: num(cond?.n_prb),
            };
          }),
        );
      } catch (e) {
        if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const counts = useMemo(() => {
    let complete = 0, warning = 0, failed = 0, other = 0;
    for (const r of rows) {
      const c = rowClass(r);
      if (c === 'complete') complete++;
      else if (c === 'warning') warning++;
      else if (c === 'failed') failed++;
      else other++;
    }
    return { complete, warning, failed, other };
  }, [rows]);

  if (loading) return <Spinner label={`loading runs + merged… ${progress ? `${progress.done}/${progress.total}` : ''}`} />;
  if (error) return <ErrorBox error={error} />;

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Experiment Matrix</div>
          <div className="subtitle">every run with its condition and energy outcome</div>
        </div>
      </div>

      <div className="stat-grid">
        <StatCard label="Complete" value={counts.complete} tone="good" icon="check" />
        <StatCard label="Warning" value={counts.warning} tone="warn" icon="info" />
        <StatCard label="Failed" value={counts.failed} tone="bad" icon="x" />
        <StatCard label="Other" value={counts.other} tone="muted" icon="grid" />
      </div>

      <Card title="Run matrix">
        {rows.length === 0 ? (
          <EmptyState>No runs.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>Run</th>
                  <th>Device</th>
                  <th>Env</th>
                  <th>RSRP (dBm)</th>
                  <th>BW (MHz)</th>
                  <th>Target SNR (dB)</th>
                  <th>MCS mode</th>
                  <th>PRB</th>
                  <th>Power (W)</th>
                  <th>Energy (J)</th>
                  <th>Quality</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.run_id} className={rowClass(r)} onClick={() => nav(`/run/${encodeURIComponent(r.run_id)}`)} style={{ cursor: 'pointer' }}>
                    <td className="mono">{r.run_id}</td>
                    <td className="mono">{r.device_id ?? '—'}</td>
                    <td><Badge tone={r.environment === 'AC' || r.environment === 'RC' ? 'accent' : 'muted'}>{r.environment ?? '—'}</Badge></td>
                    <td className="mono">{fmt(r.target_rsrp_dbm)}</td>
                    <td className="mono">{fmt(r.bandwidth_mhz)}</td>
                    <td className="mono">{r.pusch_target_snr_x10 != null ? (r.pusch_target_snr_x10 / 10).toFixed(1) : '—'}</td>
                    <td className="mono">{r.scheduler_mode ?? '—'}</td>
                    <td className="mono">{fmt(r.n_prb_cond)}</td>
                    <td className="mono">{fmt(r.phone_power_w, 4)}</td>
                    <td className="mono">{fmt(r.active_energy_j, 4)}</td>
                    <td>
                      <Badge tone={r.quality_status === 'PASS' ? 'good' : r.quality_status === 'WARNING' ? 'warn' : r.quality_status === 'FAILED' ? 'bad' : r.state === 'FAILED' ? 'bad' : r.state === 'WARNING' ? 'warn' : r.state === 'COMPLETE' ? 'good' : 'muted'}>
                        {r.quality_status ?? r.state}
                      </Badge>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
    </div>
  );
}
