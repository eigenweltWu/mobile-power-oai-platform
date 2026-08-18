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
      toast('ok', `已提取 ${runId}（${res.imported?.samples ?? 0} 个样本）`);
      loadInventory(serial);
    } catch (e) {
      toast('err', `提取失败: ${e instanceof Error ? e.message : String(e)}`);
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
          label="USB 连接"
          tone={usbAttached ? 'good' : 'bad'}
          value={usbAttached ? '已连接' : '未连接'}
          sub={`serial ${serial}`}
        />
        <StatCard icon="compare" label="交集实验" tone="accent" value={intersect.filter((e) => e.reconciliation === 'BOTH').length}
          sub={`${platformOnly.length} 个实验仅平台可见`} />
        <StatCard icon="data" label="手机 Run 总数" value={totalRuns}
          sub={phoneOnly.length ? `${phoneOnly.length} 个实验仅手机可见` : '全部与平台交集'} />
        <StatCard icon="download" label="待提取 Run" tone={unPulled > 0 ? 'warn' : 'good'}
          value={unPulled} sub={unPulled > 0 ? '手机样本多于平台' : '数据已是最新'} />
      </div>

      <Card
        title="手机数据清单（USB）"
          sub="Phone-only / Platform-only / Both 明确对账；导入按 Run ID 幂等替换平台副本，不改写手机原始数据。"
        right={
          <button className="btn sm" disabled={!usbAttached || loading}
            onClick={() => loadInventory(serial)}>
            <Icon name="refresh" size={14} /> {loading ? '读取中…' : '刷新清单'}
          </button>
        }
      >
        {!usbAttached && (
          <EmptyState>手机未通过 USB 连接。插上 USB 线并允许 ADB 调试后，清单会自动加载。</EmptyState>
        )}
        {usbAttached && loading && !inv && <Spinner label="读取手机清单…" />}
        {usbAttached && inv && !inv.ok && <ErrorBox error={inv.error} />}
        {usbAttached && inv && inv.ok && exps.length === 0 && (
          <EmptyState>手机上没有任何带数据的实验记录。</EmptyState>
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
                    {e.runs.length} runs · {e.runs.reduce((a, r) => a + (r.phone_sample_count ?? 0), 0)} 样本
                  </span>
                  <span className="ps-actions">
                    {e.reconciliation === 'BOTH' && e.runs.some((run) => run.reconciliation !== 'PLATFORM_ONLY') && (
                      <button className="btn sm primary" disabled={pulling.size > 0}
                        onClick={(ev) => { ev.stopPropagation(); pullExperiment(e.experiment_id, e.runs); }}>
                        提取全部
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
                          <th>首样本 (UTC)</th>
                          <th>末样本 (UTC)</th>
                          <th>手机样本</th>
                          <th>平台样本</th>
                          <th>平台状态</th>
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
                                  {pulling.has(key) ? '提取中…' : r.reconciliation === 'PLATFORM_ONLY' ? '手机无数据' : '提取 / 更新'}
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
