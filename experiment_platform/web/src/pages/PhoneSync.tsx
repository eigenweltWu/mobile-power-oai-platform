import { useCallback, useEffect, useRef, useState } from 'react';
import { api } from '../api';
import { fmtTs } from '../format';
import { Badge, Card, EmptyState, ErrorBox, Icon, Spinner, StatCard, toast } from '../components/ui';

/* ------------------------------------------------------------------ types */

type PhoneRun = {
  run_id: string;
  phone_sample_count: number | null;
  first_utc_ms: number | null;
  last_utc_ms: number | null;
  in_platform: boolean;
  platform_state: string | null;
  platform_started_ms: number | null;
  platform_sample_count: number;
  reconciliation: 'BOTH_MATCH' | 'BOTH_DATA_DIFFERS' | 'PHONE_ONLY' | 'PLATFORM_ONLY' | 'IDENTITY_CONFLICT';
};

type PhoneExperiment = {
  experiment_id: string;
  environment: string | null;
  phone_collection_count: number | null;
  in_platform: boolean;
  collected_count: number;
  last_collected_ms: number | null;
  runs: PhoneRun[];
  reconciliation: 'BOTH' | 'PHONE_ONLY' | 'PLATFORM_ONLY';
};

type Inventory = { ok: true; serial: string; phone_experiments: PhoneExperiment[] } | { ok: false; error: string };

type PlatformStatus = {
  phone?: { state?: string; usb_attached?: boolean; serial?: string | null };
};

/* ------------------------------------------------------------------ page */

export default function PhoneSync() {
  const [status, setStatus] = useState<PlatformStatus | null>(null);
  const [inv, setInv] = useState<Inventory | null>(null);
  const [loading, setLoading] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [pulling, setPulling] = useState<Set<string>>(new Set());
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  const serial = status?.phone?.serial || '53616213';
  const usbAttached = !!status?.phone?.usb_attached;

  const loadStatus = useCallback(() => {
    api.get<PlatformStatus>('/api/platform/status')
      .then((s) => { if (mounted.current) setStatus(s); })
      .catch(() => { if (mounted.current) setStatus(null); });
  }, []);

  const loadInventory = useCallback((ser: string) => {
    setLoading(true);
    api.get<Inventory>(`/api/phone/tasks?serial=${encodeURIComponent(ser)}`)
      .then((i) => { if (mounted.current) setInv(i); })
      .catch((e) => { if (mounted.current) setInv({ ok: false, error: String(e?.message || e) }); })
      .finally(() => { if (mounted.current) setLoading(false); });
  }, []);

  useEffect(() => {
    loadStatus();
    const t = window.setInterval(loadStatus, 5000);
    return () => window.clearInterval(t);
  }, [loadStatus]);

  // Auto-load once USB appears (or on manual refresh).
  useEffect(() => {
    if (usbAttached && inv === null && !loading) loadInventory(serial);
  }, [usbAttached, serial, inv, loading, loadInventory]);

  const toggle = (eid: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(eid)) next.delete(eid); else next.add(eid);
      return next;
    });
  };

  const pullRun = async (eid: string, runId: string) => {
    const key = `${eid}/${runId}`;
    setPulling((prev) => new Set(prev).add(key));
    try {
      const res = await api.post<{ ok: boolean; imported?: { samples?: number } }>(
        '/api/phone/pull', { experimentId: eid, runId, serial });
      toast('ok', `Imported ${runId} (${res.imported?.samples ?? 0} samples).`);
      loadInventory(serial);
    } catch (e) {
      toast('err', `Import failed: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPulling((prev) => { const n = new Set(prev); n.delete(key); return n; });
    }
  };

  const pullExperiment = async (eid: string, runs: PhoneRun[]) => {
    for (const r of runs) {
      // sequential: one adb-forward data tunnel at a time
      // eslint-disable-next-line no-await-in-loop
      await pullRun(eid, r.run_id);
    }
  };

  const exps = inv && inv.ok ? inv.phone_experiments : [];
  const intersect = exps.filter((e) => e.in_platform);
  const phoneOnly = exps.filter((e) => !e.in_platform);
  const platformOnly = exps.filter((e) => e.reconciliation === 'PLATFORM_ONLY');
  const totalRuns = exps.reduce((a, e) => a + e.runs.length, 0);
  const unPulled = intersect.reduce(
    (a, e) => a + e.runs.filter((r) => (r.phone_sample_count ?? 0) > r.platform_sample_count).length, 0);

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="grid" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))' }}>
        <StatCard
          icon="lock"
          label="USB Connection"
          tone={usbAttached ? 'good' : 'bad'}
          value={usbAttached ? 'CONNECTED' : 'DISCONNECTED'}
          sub={`serial ${serial}`}
        />
        <StatCard icon="compare" label="Matched Experiments" tone="accent" value={intersect.filter((e) => e.reconciliation === 'BOTH').length}
          sub={`${platformOnly.length} platform-only Experiments`} />
        <StatCard icon="data" label="Phone Runs" value={totalRuns}
          sub={phoneOnly.length ? `${phoneOnly.length} phone-only Experiments` : 'All Experiments reconciled'} />
        <StatCard icon="download" label="Runs to Import" tone={unPulled > 0 ? 'warn' : 'good'}
          value={unPulled} sub={unPulled > 0 ? 'Phone has additional samples' : 'Data is up to date'} />
      </div>

      <Card
        title="Phone Data Inventory (USB)"
          sub="Phone-only, Platform-only and Both are reconciled explicitly. Import replaces the platform copy idempotently by Run ID and never modifies phone source data."
        right={
          <button className="btn sm" disabled={!usbAttached || loading}
            onClick={() => loadInventory(serial)}>
            <Icon name="refresh" size={14} /> {loading ? 'Loading…' : 'Refresh Inventory'}
          </button>
        }
      >
        {!usbAttached && (
          <EmptyState>The phone is not connected over USB. Connect it and authorize ADB debugging to load the inventory.</EmptyState>
        )}
        {usbAttached && loading && !inv && <Spinner label="Loading phone inventory…" />}
        {usbAttached && inv && !inv.ok && <ErrorBox error={inv.error} />}
        {usbAttached && inv && inv.ok && exps.length === 0 && (
          <EmptyState>The phone has no Experiment records containing data.</EmptyState>
        )}
        {usbAttached && inv && inv.ok && exps.length > 0 && (
          <div className="phone-sync-list">
            {exps.map((e) => (
              <div key={e.experiment_id} className={`phone-sync-card ${e.reconciliation === 'BOTH' ? 'match' : 'phone-only'}`}>
                <div className="ps-card-head" onClick={() => toggle(e.experiment_id)}>
                  <span className="ps-caret">{expanded.has(e.experiment_id) ? '▾' : '▸'}</span>
                  <span className="mono ps-eid">{e.experiment_id}</span>
                  {e.environment ? <Badge tone={e.environment === 'RC' ? 'warn' : 'accent'}>{e.environment}</Badge> : null}
                  <Badge tone={e.reconciliation === 'BOTH' ? 'good' : 'muted'}>{e.reconciliation === 'BOTH' ? 'Both' : e.reconciliation === 'PHONE_ONLY' ? 'Phone-only' : 'Platform-only'}</Badge>
                  <span className="ps-meta">
                    {e.runs.length} Runs · {e.runs.reduce((a, r) => a + (r.phone_sample_count ?? 0), 0)} samples
                  </span>
                  <span className="ps-actions">
                    {e.reconciliation === 'BOTH' && e.runs.some((run) => run.reconciliation !== 'PLATFORM_ONLY') && (
                      <button className="btn sm primary" disabled={pulling.size > 0}
                        onClick={(ev) => { ev.stopPropagation(); pullExperiment(e.experiment_id, e.runs); }}>
                        Import All
                      </button>
                    )}
                  </span>
                </div>
                {expanded.has(e.experiment_id) && (
                  <div className="table-wrap">
                    <table className="data">
                      <thead>
                        <tr>
                          <th>run id</th>
                          <th>First Sample (UTC)</th>
                          <th>Last Sample (UTC)</th>
                          <th>Phone Samples</th>
                          <th>Platform Samples</th>
                          <th>Platform Status</th>
                          <th></th>
                        </tr>
                      </thead>
                      <tbody>
                        {e.runs.map((r) => {
                          const key = `${e.experiment_id}/${r.run_id}`;
                          const stale = r.reconciliation === 'BOTH_DATA_DIFFERS';
                          return (
                            <tr key={r.run_id} className={stale ? 'warning' : 'complete'}>
                              <td className="mono">{r.run_id}</td>
                              <td className="mono">{fmtTs(r.first_utc_ms)}</td>
                              <td className="mono">{fmtTs(r.last_utc_ms)}</td>
                              <td className="mono">{r.phone_sample_count ?? '—'}</td>
                              <td className="mono">{r.in_platform ? r.platform_sample_count : '—'}</td>
                              <td><Badge tone={r.reconciliation === 'BOTH_MATCH' ? 'good' : r.reconciliation === 'IDENTITY_CONFLICT' ? 'bad' : 'warn'}>{r.reconciliation.replace(/_/g, ' ')}</Badge>{r.platform_state ? ` · ${r.platform_state}` : ''}</td>
                              <td>
                                <button className="btn sm" disabled={pulling.has(key) || r.reconciliation === 'PLATFORM_ONLY' || r.reconciliation === 'IDENTITY_CONFLICT'}
                                  onClick={() => pullRun(e.experiment_id, r.run_id)}>
                                  {pulling.has(key) ? 'Importing…' : r.reconciliation === 'PLATFORM_ONLY' ? 'No phone data' : 'Import / Update'}
                                </button>
                              </td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                  </div>
                )}
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
