import { useEffect, useMemo, useState } from 'react';
import { api } from '../api';
import type { Condition, Experiment, Run } from '../types';
import { computeMetrics, loadMergedMap, type RunMetrics } from '../metrics';
import { Badge, Card, EmptyState, ErrorBox, Field, Spinner } from '../components/ui';
import { fmt, num } from '../format';

interface Row extends RunMetrics {
  run_id: string;
  device_id: string | null;
  environment: string | null;
  orientation_deg: number | null;
  target_rsrp_dbm: number | null;
  bandwidth_mhz: number | null;
  pusch_target_snr_x10: number | null;
  traffic_condition: string | null;
  condition_id: string;
}

function rsrpBin(v: number | null): string {
  if (v === null || !Number.isFinite(v)) return '—';
  const lo = Math.floor(v / 5) * 5;
  return `${lo}…${lo + 5}`;
}

export default function Comparison({ nav }: { nav: (p: string) => void }) {
  const [exps, setExps] = useState<Experiment[]>([]);
  const [conds, setConds] = useState<Condition[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [rows, setRows] = useState<Row[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [progress, setProgress] = useState<{ done: number; total: number } | null>(null);

  const [filters, setFilters] = useState({
    device: '',
    environment: '',
    orientation: '',
    rsrp: '',
    bandwidth: '',
    pusch: '',
    traffic: '',
  });

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
        setExps(e);
        setConds(c);
        setRuns(r);
        const envMap = new Map(e.map((x) => [x.experiment_id, x.environment]));
        const condMap = new Map(c.map((x) => [x.condition_id, x]));
        const merged = await loadMergedMap(r.map((x) => x.run_id), 4, (done, total) => {
          if (!cancelled) setProgress({ done, total });
        });
        if (cancelled) return;
        const built: Row[] = r.map((run) => {
          const cond = condMap.get(run.condition_id);
          const m = computeMetrics(merged.get(run.run_id) ?? []);
          return {
            ...m,
            run_id: run.run_id,
            condition_id: run.condition_id,
            device_id: run.device_id ?? null,
            environment: cond?.environment ?? envMap.get(run.experiment_id) ?? null,
            orientation_deg: num(cond?.orientation_deg),
            target_rsrp_dbm: num(cond?.target_rsrp_dbm),
            bandwidth_mhz: num(cond?.bandwidth_mhz),
            pusch_target_snr_x10: num(cond?.pusch_target_snr_x10),
            traffic_condition: cond?.traffic_condition ?? null,
          };
        });
        setRows(built);
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

  const devices = useMemo(() => Array.from(new Set(rows.map((r) => r.device_id).filter(Boolean))) as string[], [rows]);
  const envs = useMemo(() => Array.from(new Set(rows.map((r) => r.environment).filter(Boolean))) as string[], [rows]);
  const orientations = useMemo(() => Array.from(new Set(rows.map((r) => r.orientation_deg).filter((v) => v !== null))) as number[], [rows]);
  const rsrpBins = useMemo(() => Array.from(new Set(rows.map((r) => rsrpBin(r.target_rsrp_dbm)))).sort(), [rows]);
  const bws = useMemo(() => Array.from(new Set(rows.map((r) => r.bandwidth_mhz).filter((v) => v !== null))) as number[], [rows]);
  const puschTargets = useMemo(() => Array.from(new Set(rows.map((r) => r.pusch_target_snr_x10).filter((v) => v !== null))) as number[], [rows]);
  const traffics = useMemo(() => Array.from(new Set(rows.map((r) => r.traffic_condition).filter(Boolean))) as string[], [rows]);

  const filtered = useMemo(
    () =>
      rows.filter((r) => {
        if (filters.device && r.device_id !== filters.device) return false;
        if (filters.environment && r.environment !== filters.environment) return false;
        if (filters.orientation && r.orientation_deg !== Number(filters.orientation)) return false;
        if (filters.rsrp && rsrpBin(r.target_rsrp_dbm) !== filters.rsrp) return false;
        if (filters.bandwidth && r.bandwidth_mhz !== Number(filters.bandwidth)) return false;
        if (filters.pusch && r.pusch_target_snr_x10 !== Number(filters.pusch)) return false;
        if (filters.traffic && r.traffic_condition !== filters.traffic) return false;
        return true;
      }),
    [rows, filters],
  );

  const setF = (k: keyof typeof filters, v: string) => setFilters((f) => ({ ...f, [k]: v }));

  if (loading) {
    return (
      <div className="stack">
        <Spinner label={`loading runs + merged data… ${progress ? `${progress.done}/${progress.total}` : ''}`} />
      </div>
    );
  }
  if (error) return <ErrorBox error={error} />;

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">AC/RC Comparison</div>
          <div className="subtitle">quick per-condition energy check across runs</div>
        </div>
        <div className="row gap-sm">
          <Badge tone="accent">{filtered.length} conditions</Badge>
          <Badge tone="muted">{rows.length} runs total</Badge>
        </div>
      </div>

      <Card title="Filters">
        <div className="grid cols-4">
          <Field label="device">
            <select value={filters.device} onChange={(e) => setF('device', e.target.value)}>
              <option value="">all</option>
              {devices.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="AC/RC">
            <select value={filters.environment} onChange={(e) => setF('environment', e.target.value)}>
              <option value="">all</option>
              {envs.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="orientation (°)">
            <select value={filters.orientation} onChange={(e) => setF('orientation', e.target.value)}>
              <option value="">all</option>
              {orientations.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="RSRP bin (dBm)">
            <select value={filters.rsrp} onChange={(e) => setF('rsrp', e.target.value)}>
              <option value="">all</option>
              {rsrpBins.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="bandwidth (MHz)">
            <select value={filters.bandwidth} onChange={(e) => setF('bandwidth', e.target.value)}>
              <option value="">all</option>
              {bws.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="PUSCH target (X10)">
            <select value={filters.pusch} onChange={(e) => setF('pusch', e.target.value)}>
              <option value="">all</option>
              {puschTargets.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
          <Field label="traffic">
            <select value={filters.traffic} onChange={(e) => setF('traffic', e.target.value)}>
              <option value="">all</option>
              {traffics.map((d) => <option key={d} value={d}>{d}</option>)}
            </select>
          </Field>
        </div>
      </Card>

      <Card title="Per-condition metrics">
        {filtered.length === 0 ? (
          <EmptyState>No conditions match the filters.</EmptyState>
        ) : (
          <div className="table-wrap">
            <table className="data">
              <thead>
                <tr>
                  <th>condition</th>
                  <th>device</th>
                  <th>env</th>
                  <th>orient</th>
                  <th>RSRP</th>
                  <th>BW</th>
                  <th>target SNR</th>
                  <th>traffic</th>
                  <th>phone power (W)</th>
                  <th>active energy (J)</th>
                  <th>energy/bit (J/bit)</th>
                  <th>PH (dB)</th>
                  <th>TPC+ ratio</th>
                  <th>HARQ retx</th>
                  <th>MCS</th>
                  <th>N_PRB</th>
                  <th>PUSCH SNR (dB)</th>
                </tr>
              </thead>
              <tbody>
                {filtered.map((r) => (
                  <tr key={r.run_id} onClick={() => nav(`/run/${encodeURIComponent(r.run_id)}`)} style={{ cursor: 'pointer' }}>
                    <td className="mono">{r.condition_id}</td>
                    <td className="mono">{r.device_id ?? '—'}</td>
                    <td><Badge tone={r.environment === 'AC' || r.environment === 'RC' ? 'accent' : 'muted'}>{r.environment ?? '—'}</Badge></td>
                    <td className="mono">{fmt(r.orientation_deg)}</td>
                    <td className="mono">{fmt(r.target_rsrp_dbm)}</td>
                    <td className="mono">{fmt(r.bandwidth_mhz)}</td>
                    <td className="mono">{r.pusch_target_snr_x10 != null ? (r.pusch_target_snr_x10 / 10).toFixed(1) : '—'}</td>
                    <td>{r.traffic_condition ?? '—'}</td>
                    <td className="mono">{fmt(r.phone_power_w, 4)}</td>
                    <td className="mono">{fmt(r.active_energy_j, 4)}</td>
                    <td className="mono">{r.energy_per_bit != null ? r.energy_per_bit.toExponential(2) : '—'}</td>
                    <td className="mono">{fmt(r.ph_normalized, 3)}</td>
                    <td className="mono">{fmt(r.tpc_positive_ratio, 3)}</td>
                    <td className="mono">{fmt(r.harq_retx_ratio, 3)}</td>
                    <td className="mono">{fmt(r.mcs, 3)}</td>
                    <td className="mono">{fmt(r.n_prb, 3)}</td>
                    <td className="mono">{fmt(r.pusch_snr, 3)}</td>
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
