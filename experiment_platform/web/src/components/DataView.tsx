import { useEffect, useMemo, useState } from 'react';
import { num } from '../format';

/** Render an array of objects as a monospace data table with dynamic columns. */
export function DataTable({
  rows,
  columns,
  maxRows = 200,
}: {
  rows: Record<string, unknown>[];
  columns?: string[];
  maxRows?: number;
}) {
  const cols = useMemo(() => {
    if (columns) return columns;
    const seen = new Set<string>();
    for (const r of rows) for (const k of Object.keys(r)) seen.add(k);
    return Array.from(seen);
  }, [rows, columns]);

  if (rows.length === 0) return <div className="notice-box">No rows.</div>;
  const shown = rows.slice(0, maxRows);

  return (
    <div className="table-wrap">
      <table className="data">
        <thead>
          <tr>
            <th>#</th>
            {cols.map((c) => (
              <th key={c}>{c}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {shown.map((r, i) => (
            <tr key={i}>
              <td className="mono">{i + 1}</td>
              {cols.map((c) => (
                <td key={c} className="mono">
                  {cellText(r[c])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {rows.length > maxRows && (
        <div className="notice-box" style={{ marginTop: 8 }}>
          Showing {maxRows} of {rows.length} rows.
        </div>
      )}
    </div>
  );
}

function cellText(v: unknown): string {
  if (v === null || v === undefined) return '·';
  if (typeof v === 'number') {
    if (Number.isInteger(v)) return String(v);
    return v.toPrecision(6);
  }
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

/** Pretty-printed raw JSON (monospace, scrollable). */
export function RawJson({ data, label }: { data: unknown; label?: string }) {
  return (
    <div>
      {label && (
        <div className="row" style={{ marginBottom: 6 }}>
          <span className="badge accent">{label}</span>
        </div>
      )}
      <pre className="raw">{JSON.stringify(data, null, 2)}</pre>
    </div>
  );
}

/** A small client-side "tabs" wrapper. */
export function TabBar<T extends string>({
  tabs,
  active,
  onChange,
}: {
  tabs: { id: T; label: string }[];
  active: T;
  onChange: (t: T) => void;
}) {
  return (
    <div className="tabs">
      {tabs.map((t) => (
        <button key={t.id} className={t.id === active ? 'active' : ''} onClick={() => onChange(t.id)}>
          {t.label}
        </button>
      ))}
    </div>
  );
}

/** A generic async-resource loading hook (loads once, with manual reload). */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[]): {
  data: T | null;
  error: Error | null;
  loading: boolean;
  reload: () => void;
  setData: (d: T | null) => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    loader()
      .then((d) => {
        if (!cancelled) setData(d);
      })
      .catch((e) => {
        if (!cancelled) setError(e instanceof Error ? e : new Error(String(e)));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading, reload: () => setTick((t) => t + 1), setData };
}

/** Numeric summarizer used across pages. */
export function summarize(values: (number | null)[]): {
  n: number;
  mean: number | null;
  median: number | null;
  min: number | null;
  max: number | null;
  sum: number | null;
} {
  const xs = values.filter((v): v is number => v !== null && Number.isFinite(v));
  if (xs.length === 0) return { n: 0, mean: null, median: null, min: null, max: null, sum: null };
  const sorted = [...xs].sort((a, b) => a - b);
  const median = sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
  return {
    n: xs.length,
    mean: xs.reduce((a, b) => a + b, 0) / xs.length,
    median,
    min: sorted[0],
    max: sorted[sorted.length - 1],
    sum: xs.reduce((a, b) => a + b, 0),
  };
}

export function colNums(rows: Record<string, unknown>[], key: string): (number | null)[] {
  return rows.map((r) => num(r[key]));
}
