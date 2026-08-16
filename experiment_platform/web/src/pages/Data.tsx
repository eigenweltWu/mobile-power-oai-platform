import { useMemo, useState } from 'react';
import { api, downloadFile } from '../api';
import type { MergedRow } from '../types';
import { Badge, Card, EmptyState, ErrorBox, Field, Spinner } from '../components/ui';
import { DataTable, RawJson, TabBar, useLoad } from '../components/DataView';
import { fmtDurationMs, fmtFixed, num } from '../format';

type Tab = 'phone' | 'snapshots' | 'events' | 'merged' | 'summary';

function flatten(obj: Record<string, unknown>, prefix = '', out: Record<string, unknown> = {}): Record<string, unknown> {
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === 'object' && !Array.isArray(v)) flatten(v as Record<string, unknown>, key, out);
    else out[key] = v;
  }
  return out;
}

const PHONE_RAW_FILES = ['phone_samples.csv', 'phone_events.csv', 'phone_session.json', 'phone_sync.json'];

export default function Data({ nav }: { nav: (p: string) => void }) {
  const [tab, setTab] = useState<Tab>('merged');
  const [runId, setRunId] = useState('');
  const [rawMode, setRawMode] = useState<'table' | 'json'>('table');

  const uesLoad = useLoad<Record<string, unknown>>(
    () => (tab === 'snapshots' ? api.get('/api/oai/research/ues') : Promise.resolve({})),
    [tab],
  );
  const eventsLoad = useLoad<Record<string, unknown>>(
    () => (tab === 'events' ? api.get('/api/oai/research/events?limit=200') : Promise.resolve({})),
    [tab],
  );
  const mergedLoad = useLoad<MergedRow[]>(
    () => (tab === 'merged' || tab === 'summary' ? (runId ? api.get(`/api/runs/${encodeURIComponent(runId)}/merged`) : Promise.reject(new Error('enter run_id'))) : Promise.resolve([])),
    [tab, runId],
  );

  const summary = useMemo(() => computeSummary(mergedLoad.data ?? []), [mergedLoad.data]);

  const uesRows = useMemo(() => {
    const ues = uesLoad.data?.ues;
    if (Array.isArray(ues)) return ues.map((u) => flatten(u as Record<string, unknown>));
    return [];
  }, [uesLoad.data]);

  const eventRows = useMemo(() => {
    const ev = eventsLoad.data?.events;
    if (Array.isArray(ev)) return ev as Record<string, unknown>[];
    return [];
  }, [eventsLoad.data]);

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Data Browser</div>
          <div className="subtitle">raw phone / OAI records and merged 1s summaries</div>
        </div>
      </div>

      <Card title="Browser">
        <div className="row" style={{ marginBottom: 12 }}>
          <Field label="run_id (for merged / summary)">
            <input className="mono" style={{ width: 200 }} value={runId} placeholder="e.g. R001" onChange={(e) => setRunId(e.target.value)} />
          </Field>
          {runId && (
            <button className="btn" onClick={() => nav(`/run/${encodeURIComponent(runId)}`)}>
              Run Detail →
            </button>
          )}
        </div>
        <TabBar<Tab>
          tabs={[
            { id: 'phone', label: 'Raw Phone' },
            { id: 'snapshots', label: 'Raw OAI Snapshots' },
            { id: 'events', label: 'Raw OAI Events' },
            { id: 'merged', label: 'Merged 1s' },
            { id: 'summary', label: 'Run Summary' },
          ]}
          active={tab}
          onChange={setTab}
        />
      </Card>

      {tab === 'phone' && (
        <Card title="Raw Phone (Level 0)">
          <div className="notice-box">
            The backend does not expose a per-run raw phone REST endpoint. Raw phone files are stored on the platform filesystem and
            are included in the experiment ZIP export under <span className="mono">raw/phone/&lt;run_id&gt;/</span>. Use the{' '}
            <b>Export</b> tab (or the Export ZIP button on the Experiment page) to retrieve them.
          </div>
          <div className="row gap-sm" style={{ marginTop: 12 }}>
            {PHONE_RAW_FILES.map((f) => (
              <Badge key={f} tone="muted">{f}</Badge>
            ))}
          </div>
        </Card>
      )}

      {tab === 'snapshots' && (
        <Card
          title="Raw OAI Snapshots (/api/oai/research/ues)"
          right={
            <div className="row gap-sm">
              <button className="btn" onClick={() => setRawMode(rawMode === 'table' ? 'json' : 'table')}>
                {rawMode === 'table' ? 'show raw JSON' : 'show table'}
              </button>
              <button className="btn" onClick={uesLoad.reload}>Refresh</button>
            </div>
          }
        >
          {uesLoad.loading ? (
            <Spinner />
          ) : uesLoad.error ? (
            <ErrorBox error={uesLoad.error} />
          ) : rawMode === 'json' ? (
            <RawJson data={uesLoad.data} />
          ) : (
            <>
              {uesLoad.data && (
                <div className="row gap-sm" style={{ marginBottom: 10 }}>
                  <Badge tone="accent">{uesRows.length} UE(s)</Badge>
                  {(uesLoad.data as Record<string, unknown>).collection != null && (
                    <Badge tone="muted">collection {String(JSON.stringify((uesLoad.data as Record<string, unknown>).collection) ?? '')}</Badge>
                  )}
                </div>
              )}
              {uesRows.length === 0 ? <EmptyState>No snapshots.</EmptyState> : <DataTable rows={uesRows} />}
            </>
          )}
        </Card>
      )}

      {tab === 'events' && (
        <Card
          title="Raw OAI Events (/api/oai/research/events)"
          right={
            <div className="row gap-sm">
              <button className="btn" onClick={() => setRawMode(rawMode === 'table' ? 'json' : 'table')}>
                {rawMode === 'table' ? 'show raw JSON' : 'show table'}
              </button>
              <button className="btn" onClick={eventsLoad.reload}>Refresh</button>
            </div>
          }
        >
          {eventsLoad.loading ? (
            <Spinner />
          ) : eventsLoad.error ? (
            <ErrorBox error={eventsLoad.error} />
          ) : rawMode === 'json' ? (
            <RawJson data={eventsLoad.data} />
          ) : (
            <>
              {eventsLoad.data && (
                <div className="row gap-sm" style={{ marginBottom: 10 }}>
                  <Badge tone="accent">{eventRows.length} events</Badge>
                  <Badge tone="muted">count {String((eventsLoad.data as Record<string, unknown>).count ?? '—')}</Badge>
                </div>
              )}
              {eventRows.length === 0 ? <EmptyState>No events.</EmptyState> : <DataTable rows={eventRows} maxRows={500} />}
            </>
          )}
        </Card>
      )}

      {tab === 'merged' && (
        <Card
          title="Merged 1s (/api/runs/{run_id}/merged)"
          right={
            runId ? (
              <button className="btn" onClick={() => downloadFile(`/api/runs/${encodeURIComponent(runId)}/merged.csv`, `${runId}_merged_1s.csv`)}>
                merged.csv ↓
              </button>
            ) : undefined
          }
        >
          {!runId ? (
            <EmptyState>Enter a run_id above.</EmptyState>
          ) : mergedLoad.loading ? (
            <Spinner />
          ) : mergedLoad.error ? (
            <ErrorBox error={mergedLoad.error} />
          ) : (
            <>
              <div className="row gap-sm" style={{ marginBottom: 10 }}>
                <Badge tone="accent">{mergedLoad.data?.length ?? 0} windows</Badge>
              </div>
              <DataTable rows={mergedLoad.data ?? []} maxRows={1000} />
            </>
          )}
        </Card>
      )}

      {tab === 'summary' && (
        <Card title="Run Summary (computed from merged 1s)">
          {!runId ? (
            <EmptyState>Enter a run_id above.</EmptyState>
          ) : mergedLoad.loading ? (
            <Spinner />
          ) : mergedLoad.error ? (
            <ErrorBox error={mergedLoad.error} />
          ) : (
            <div className="kv">
              <dt>windows</dt><dd>{summary.windows}</dd>
              <dt>duration</dt><dd>{summary.duration}</dd>
              <dt>phases</dt><dd>{summary.phases.join(', ') || '—'}</dd>
              <dt>baseline energy (J)</dt><dd>{fmtFixed(summary.baseline_energy, 3)}</dd>
              <dt>active energy (J)</dt><dd>{fmtFixed(summary.active_energy, 3)}</dd>
              <dt>tail energy (J)</dt><dd>{fmtFixed(summary.tail_energy, 3)}</dd>
              <dt>total energy (J)</dt><dd>{fmtFixed(summary.total_energy, 3)}</dd>
              <dt>mean active power (W)</dt><dd>{fmtFixed(summary.active_power, 4)}</dd>
              <dt>energy per bit (J/bit)</dt><dd>{summary.energy_per_bit != null ? summary.energy_per_bit.toExponential(2) : '—'}</dd>
              <dt>mean PUSCH SNR (dB)</dt><dd>{fmtFixed(summary.pusch_snr, 3)}</dd>
              <dt>mean PH normalized (dB)</dt><dd>{fmtFixed(summary.ph_norm, 3)}</dd>
              <dt>TPC positive ratio</dt><dd>{fmtFixed(summary.tpc_pos, 3)}</dd>
              <dt>HARQ retx ratio</dt><dd>{fmtFixed(summary.harq, 3)}</dd>
            </div>
          )}
        </Card>
      )}
    </div>
  );
}

interface Summary {
  windows: number;
  duration: string;
  phases: string[];
  baseline_energy: number | null;
  active_energy: number | null;
  tail_energy: number | null;
  total_energy: number | null;
  active_power: number | null;
  energy_per_bit: number | null;
  pusch_snr: number | null;
  ph_norm: number | null;
  tpc_pos: number | null;
  harq: number | null;
}

function computeSummary(rows: MergedRow[]): Summary {
  if (rows.length === 0) {
    return {
      windows: 0, duration: '—', phases: [],
      baseline_energy: null, active_energy: null, tail_energy: null, total_energy: null,
      active_power: null, energy_per_bit: null, pusch_snr: null, ph_norm: null, tpc_pos: null, harq: null,
    };
  }
  const sum = (pred: (r: MergedRow) => boolean) =>
    rows.filter(pred).reduce((a, r) => a + (num(r.phone_energy_j) ?? 0), 0);
  const mean = (pred: (r: MergedRow) => boolean, key: string) => {
    const xs = rows.filter(pred).map((r) => num(r[key])).filter((v): v is number => v !== null && Number.isFinite(v));
    return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  };
  const phases = Array.from(new Set(rows.map((r) => r.phase).filter(Boolean))) as string[];
  const baseline = sum((r) => r.phase === 'BASELINE');
  const active = sum((r) => r.phase === 'ACTIVE');
  const tail = sum((r) => r.phase === 'TAIL');
  const activePower = mean((r) => r.phase === 'ACTIVE', 'phone_power_w_mean');
  const bits = rows
    .filter((r) => r.phase === 'ACTIVE')
    .reduce((a, r) => a + ((num(r.gnb_ul_goodput_mbps) ?? 0) + (num(r.gnb_dl_goodput_mbps) ?? 0)) * 1e6, 0);
  return {
    windows: rows.length,
    duration: fmtDurationMs(rows[rows.length - 1].window_ms - rows[0].window_ms),
    phases,
    baseline_energy: baseline,
    active_energy: active,
    tail_energy: tail,
    total_energy: baseline + active + tail,
    active_power: activePower,
    energy_per_bit: bits > 0 ? active / bits : null,
    pusch_snr: mean(() => true, 'gnb_pusch_snr_db'),
    ph_norm: mean(() => true, 'gnb_ph_normalized_db'),
    tpc_pos: mean(() => true, 'tpc_positive_ratio'),
    harq: mean(() => true, 'gnb_harq_retransmission_ratio'),
  };
}
