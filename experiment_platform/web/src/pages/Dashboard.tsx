import { useCallback, useEffect, useState } from 'react';
import { api } from '../api';
import type { PlatformStatus } from '../types';
import { Badge, Card, ErrorBox, Spinner, StatCard, toast } from '../components/ui';
import { fmtBytes } from '../format';

function phoneView(status: PlatformStatus): { connected: boolean; attached: boolean; device: string | null } {
  const connected = status.phone.state?.toUpperCase() === 'CONNECTED';
  const attached = status.phone.usb_attached === true;
  return { connected, attached, device: status.phone.device };
}

function oaiView(status: PlatformStatus) {
  const o = status.oai;
  const freq = o.frequency_mhz != null ? `${o.frequency_mhz.toFixed(2)} MHz` : '—';
  const bw = o.bandwidth_mhz != null ? `${o.bandwidth_mhz} MHz` : '—';
  const stale = o.research_stale === true ? ' · research stale' : o.research_stale === false ? ' · research fresh' : '';
  const healthy = o.healthy;
  const gnb = o.gnb_running;
  const ue = o.ue_in_sync;
  return {
    label: healthy ? 'HEALTHY' : 'UNREACHABLE',
    tone: healthy ? ('good' as const) : ('bad' as const),
    sub: `${gnb ? 'gNB running' : 'gNB stopped'} · ${ue ? 'UE in-sync' : 'UE out-of-sync'}${stale} · ${freq} / ${bw}`,
  };
}

export default function Dashboard() {
  const [status, setStatus] = useState<PlatformStatus | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [lastUpdated, setLastUpdated] = useState<number | null>(null);
  const [stopping, setStopping] = useState(false);

  const load = useCallback(() => {
    api
      .get<PlatformStatus>('/api/platform/status')
      .then((s) => {
        setStatus(s);
        setError(null);
        setLastUpdated(Date.now());
      })
      .catch((e) => {
        setError(e instanceof Error ? e : new Error(String(e)));
      });
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 3000);
    return () => clearInterval(t);
  }, [load]);

  const stopRun = async (runId: string) => {
    setStopping(true);
    try {
      const r = await api.post<any>(`/api/runs/${encodeURIComponent(runId)}/stop`);
      const parts: string[] = [];
      if (r.gnb_stopped === true) parts.push('gNB stopped');
      if (r.gnb_stopped === false) parts.push('gNB stop failed');
      if (r.phone_stop_ms) parts.push(`phone stopped ${new Date(r.phone_stop_ms).toLocaleTimeString()}`);
      if (r.phone_stop_error) parts.push(`phone: ${r.phone_stop_error}`);
      toast('ok', parts.length ? `Run stopped — ${parts.join(', ')}` : 'Run stopped');
      load();
    } catch (e) {
      toast('err', e instanceof Error ? e.message : String(e));
    } finally {
      setStopping(false);
    }
  };

  if (!status && !error) return <Spinner label="loading platform status…" />;
  if (error && !status) return <ErrorBox error={error} />;

  const phone = status ? phoneView(status) : null;
  const oai = status ? oaiView(status) : null;
  const run = status?.experiment.latest_run ?? null;
  const clockOffset = status?.clock.offset_ms ?? null;
  const storage = status?.storage ?? { n_files: 0, bytes: 0 };

  return (
    <div className="stack">
      <div className="page-head">
        <div>
          <div className="title">Platform status</div>
          <div className="subtitle">
            {lastUpdated ? `updated ${new Date(lastUpdated).toLocaleTimeString()}` : 'live overview of the OAI / phone rig'}
          </div>
        </div>
        <button className="btn" onClick={load}>Refresh</button>
      </div>

      {error && <ErrorBox error={error} />}

      <div className="stat-grid">
        <StatCard
          label="Phone"
          value={
            <div className="row" style={{ gap: 6, flexWrap: 'wrap' }}>
              {phone?.connected ? <Badge tone="good">CONNECTED</Badge> : null}
              {phone?.attached ? <Badge tone="accent">ATTACHED</Badge> : null}
              {!phone?.connected && !phone?.attached ? <Badge tone="muted">OFFLINE</Badge> : null}
            </div>
          }
          sub={phone?.device ?? 'no USB, no 5G link'}
          tone={phone?.connected ? 'good' : phone?.attached ? 'accent' : 'muted'}
          icon="play"
        />
        <StatCard label="OAI Core" value={oai?.label ?? '—'} sub={oai?.sub} tone={oai?.tone ?? 'muted'} icon="flask" />
        <StatCard
          label="Clock"
          value={clockOffset === null ? 'not synced' : `${clockOffset.toFixed(2)} ms`}
          sub={status?.clock.state ?? ''}
          tone={clockOffset === null ? 'muted' : 'good'}
          icon="refresh"
        />
        <StatCard label="Storage" value={fmtBytes(storage.bytes)} sub={`${storage.n_files} files`} tone="accent" icon="data" />
      </div>

      <div className="grid cols-2">
        <Card
          title="Current Run"
          sub="latest run in the database"
          right={run ? (
            <button className="btn" disabled={stopping} onClick={() => stopRun(run.run_id)}>
              {stopping ? 'Stopping…' : 'Stop'}
            </button>
          ) : undefined}
        >
          {run ? (
            <div className="kv">
              <dt>run_id</dt>
              <dd>{run.run_id}</dd>
              <dt>state</dt>
              <dd>
                <Badge tone={run.state === 'FAILED' ? 'bad' : run.state === 'WARNING' ? 'warn' : run.state === 'COMPLETE' ? 'good' : 'accent'}>
                  {run.state}
                </Badge>
              </dd>
              <dt>experiment</dt>
              <dd>{run.experiment_id}</dd>
              <dt>condition</dt>
              <dd>{run.condition_id}</dd>
              {run.state === 'FAILED' && run.last_error ? (
                <>
                  <dt>error</dt>
                  <dd style={{ color: 'var(--bad)' }}>{run.last_error}</dd>
                </>
              ) : null}
            </div>
          ) : (
            <div className="empty-state">No runs yet.</div>
          )}
        </Card>

        <Card title="OAI Radio" sub="live gNB configuration snapshot">
          <div className="kv">
            <dt>frequency</dt>
            <dd>{status?.oai.frequency_mhz != null ? `${status.oai.frequency_mhz.toFixed(3)} MHz` : '—'}</dd>
            <dt>bandwidth</dt>
            <dd>{status?.oai.bandwidth_mhz != null ? `${status.oai.bandwidth_mhz} MHz` : '—'}</dd>
            <dt>gNB status</dt>
            <dd>{status?.oai.gnb_status ?? '—'}</dd>
            <dt>UE in-sync</dt>
            <dd>{status?.oai.ue_in_sync ? 'yes' : 'no'}</dd>
            <dt>research collection</dt>
            <dd>
              {status?.oai.research_stale === true
                ? 'stale'
                : status?.oai.research_stale === false
                  ? 'fresh'
                  : '—'}
            </dd>
          </div>
        </Card>
      </div>

      <Card title="Legend">
        <div className="row" style={{ fontSize: 12, color: 'var(--muted)' }}>
          <Badge tone="good">CONNECTED</Badge>
          <span>— in experiment with ACK (5G link active).</span>
          <Badge tone="accent">ATTACHED</Badge>
          <span>— phone connected via USB.</span>
          <Badge tone="muted">OFFLINE</Badge>
          <span>— no USB, no 5G link (after experiment ends the phone returns to OFFLINE).</span>
        </div>
      </Card>
    </div>
  );
}
