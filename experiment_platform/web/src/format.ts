/** Number / value formatting helpers (research-instrument style). */

/** Coerce an arbitrary API value into a finite number or null. */
export function num(v: unknown): number | null {
  if (typeof v === 'number') return Number.isFinite(v) ? v : null;
  if (typeof v === 'string' && v.trim() !== '') {
    const n = Number(v);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

export function fmt(v: unknown, digits = 3): string {
  const n = num(v);
  if (n === null) return '—';
  if (Number.isInteger(n) && Math.abs(n) < 1e9) return String(n);
  return n.toPrecision(digits);
}

export function fmtFixed(v: unknown, digits = 2): string {
  const n = num(v);
  if (n === null) return '—';
  return n.toFixed(digits);
}

export function fmtBytes(n: number): string {
  if (!Number.isFinite(n)) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function fmtTs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  const d = new Date(ms);
  return d.toISOString().replace('T', ' ').slice(0, 19);
}

export function fmtIso(iso: string | null | undefined): string {
  if (!iso) return '—';
  return iso.replace('T', ' ').slice(0, 19);
}

export function fmtDurationMs(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—';
  const s = ms / 1000;
  if (s < 120) return `${s.toFixed(1)} s`;
  const m = Math.floor(s / 60);
  const r = Math.round(s % 60);
  return `${m}m ${r}s`;
}

/** window_ms -> seconds since the first window (for a shared chart time axis). */
export function relSeconds(windowMs: number, t0: number): number {
  return (windowMs - t0) / 1000;
}
