import { api } from './api';
import type { MergedRow } from './types';
import { num } from './format';

export interface RunMetrics {
  phone_power_w: number | null;
  active_energy_j: number | null;
  total_energy_j: number | null;
  energy_per_bit: number | null;
  ph_normalized: number | null;
  tpc_positive_ratio: number | null;
  harq_retx_ratio: number | null;
  mcs: number | null;
  n_prb: number | null;
  pusch_snr: number | null;
  phone_rsrp: number | null;
}

function mean(xs: (number | null)[]): number | null {
  const v = xs.filter((x): x is number => x !== null && Number.isFinite(x));
  if (v.length === 0) return null;
  return v.reduce((a, b) => a + b, 0) / v.length;
}

function median(xs: (number | null)[]): number | null {
  const v = xs.filter((x): x is number => x !== null && Number.isFinite(x)).sort((a, b) => a - b);
  if (v.length === 0) return null;
  const m = Math.floor(v.length / 2);
  return v.length % 2 ? v[m] : (v[m - 1] + v[m]) / 2;
}

function mode(xs: (number | null)[]): number | null {
  const v = xs.filter((x): x is number => x !== null && Number.isFinite(x));
  if (v.length === 0) return null;
  const counts = new Map<number, number>();
  for (const x of v) counts.set(x, (counts.get(x) ?? 0) + 1);
  let best: number | null = null;
  let bestN = -1;
  for (const [x, n] of counts) {
    if (n > bestN) {
      bestN = n;
      best = x;
    }
  }
  return best;
}

/** Aggregate per-run metrics from merged 1s rows (for quick comparison / matrix). */
export function computeMetrics(rows: MergedRow[]): RunMetrics {
  if (rows.length === 0) {
    return {
      phone_power_w: null, active_energy_j: null, total_energy_j: null, energy_per_bit: null,
      ph_normalized: null, tpc_positive_ratio: null, harq_retx_ratio: null, mcs: null,
      n_prb: null, pusch_snr: null, phone_rsrp: null,
    };
  }
  const active = rows.filter((r) => r.phase === 'ACTIVE');
  const src = active.length ? active : rows;

  const powerMean = mean(src.map((r) => num(r.phone_power_w_mean)));
  const activeEnergy = rows
    .filter((r) => r.phase === 'ACTIVE')
    .reduce((acc, r) => acc + (num(r.phone_energy_j) ?? 0), 0);
  const totalEnergy = rows.reduce((acc, r) => acc + (num(r.phone_energy_j) ?? 0), 0);

  const bits = src.reduce((acc, r) => {
    const ul = num(r.gnb_ul_goodput_mbps) ?? 0;
    const dl = num(r.gnb_dl_goodput_mbps) ?? 0;
    return acc + (ul + dl) * 1e6; // Mbps * 1 s window
  }, 0);

  return {
    phone_power_w: powerMean,
    active_energy_j: active.length ? activeEnergy : null,
    total_energy_j: totalEnergy,
    energy_per_bit: bits > 0 ? activeEnergy / bits : null,
    ph_normalized: mean(src.map((r) => num(r.gnb_ph_normalized_db))),
    tpc_positive_ratio: mean(src.map((r) => num(r.tpc_positive_ratio))),
    harq_retx_ratio: mean(src.map((r) => num(r.gnb_harq_retransmission_ratio))),
    mcs: mode(src.map((r) => num(r.gnb_ul_mcs) ?? num(r.ul_mcs_mode))),
    n_prb: mean(src.map((r) => num(r.gnb_n_prb) ?? num(r.n_prb_mean))),
    pusch_snr: mean(src.map((r) => num(r.gnb_pusch_snr_db))),
    phone_rsrp: median(src.map((r) => num(r.phone_rsrp_dbm_median))),
  };
}

/** Fetch merged 1s rows for many runs with limited concurrency; 404s are tolerated (empty). */
export async function loadMergedMap(
  runIds: string[],
  concurrency = 4,
  onProgress?: (done: number, total: number) => void,
): Promise<Map<string, MergedRow[]>> {
  const out = new Map<string, MergedRow[]>();
  let done = 0;
  const queue = [...runIds];
  const worker = async () => {
    while (queue.length) {
      const id = queue.shift()!;
      try {
        out.set(id, await api.get<MergedRow[]>(`/api/runs/${encodeURIComponent(id)}/merged`));
      } catch {
        out.set(id, []);
      }
      done += 1;
      onProgress?.(done, runIds.length);
    }
  };
  await Promise.all(Array.from({ length: Math.max(1, concurrency) }, worker));
  return out;
}
